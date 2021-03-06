# Amara, universalsubtitles.org
#
# Copyright (C) 2013 Participatory Culture Foundation
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see
# http://www.gnu.org/licenses/agpl-3.0.html.

import datetime
import string
import urllib, urllib2
from collections import namedtuple

import simplejson as json
from babelsubs.storage import diff as diff_subs
from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.contrib.sites.models import Site
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from apps.videos.templatetags.paginator import paginate
from django.core.urlresolvers import reverse
from django.db.models import Sum
from django.http import (HttpResponse, Http404, HttpResponseRedirect,
                         HttpResponseForbidden)
from django.shortcuts import (render, render_to_response, get_object_or_404,
                              redirect)
from django.template import RequestContext
from django.utils.encoding import force_unicode
from django.utils.http import urlquote_plus
from django.utils.translation import ugettext, ugettext_lazy as _
from django.views.decorators.http import require_POST
from django.views.generic.list_detail import object_list
from gdata.service import RequestError
from vidscraper.errors import Error as VidscraperError

import widget
from apps.auth.models import CustomUser as User
from apps.statistic.models import EmailShareStatistic
from apps.subtitles import models as sub_models
from apps.subtitles.forms import SubtitlesUploadForm
from apps.subtitles.pipeline import rollback_to
from apps.teams.models import Task
from apps.videos import permissions
from apps.videos.decorators import get_video_revision, get_video_from_code
from apps.videos.forms import (
    VideoForm, FeedbackForm, EmailFriendForm, UserTestResultForm,
    CreateVideoUrlForm, TranscriptionFileForm, AddFromFeedForm,
    ChangeVideoOriginalLanguageForm
)
from apps.videos.models import (
    Video, Action, SubtitleLanguage, VideoUrl, AlreadyEditingException
)
from apps.videos.rpc import VideosApiClass
from apps.videos.search_indexes import VideoIndex
from apps.videos.share_utils import _add_share_panel_context_for_video, _add_share_panel_context_for_history
from apps.videos.tasks import video_changed_tasks
from apps.widget.views import base_widget_params
from externalsites.models import can_sync_videourl
from utils import send_templated_email
from utils.basexconverter import base62
from utils.decorators import never_in_prod
from utils.metrics import Meter
from utils.rpc import RpcRouter
from utils.translation import get_user_languages_from_request

from teams.permissions import can_edit_video, can_add_version, can_rollback_language

rpc_router = RpcRouter('videos:rpc_router', {
    'VideosApi': VideosApiClass()
})


# .e.g json, nor include aliases
AVAILABLE_SUBTITLE_FORMATS_FOR_DISPLAY = [
    'dfxp',  'sbv', 'srt', 'ssa', 'txt', 'vtt',
]

LanguageListItem = namedtuple("LanguageListItem", "name status tags url")

class LanguageList(object):
    """List of languages for the video pages."""

    def __init__(self, video):
        original_languages = []
        other_languages = []
        for lang in video.all_subtitle_languages():
            if lang.get_tip(public=False) is None:
                # no versions in this language yet
                continue
            language_name = lang.get_language_code_display()
            status = self._calc_status(lang)
            tags = self._calc_tags(lang)
            url = lang.get_absolute_url()
            item = LanguageListItem(language_name, status, tags, url)
            if lang.language_code == video.primary_audio_language_code:
                original_languages.append(item)
            else:
                other_languages.append(item)
        original_languages.sort(key=lambda li: li.name)
        other_languages.sort(key=lambda li: li.name)
        self.items = original_languages + other_languages

    def _calc_status(self, lang):
        if lang.subtitles_complete:
            if lang.has_public_version():
                return 'complete'
            else:
                return 'needs-review'
        else:
            if lang.is_synced(public=False):
                return 'incomplete'
            else:
                return 'needs-timing'

    def _calc_tags(self, lang):
        tags = []
        if lang.is_primary_audio_language():
            tags.append(ugettext(u'original'))

        team_video = lang.video.get_team_video()

        if not lang.subtitles_complete:
            tags.append(ugettext(u'incomplete'))
        elif team_video is not None:
            # subtiltes are complete, check if they are under review/approval.
            incomplete_tasks = (Task.objects.incomplete()
                                            .filter(team_video=team_video,
                                                    language=lang.language_code))
            for t in incomplete_tasks:
                if t.type == Task.TYPE_IDS['Review']:
                    tags.append(ugettext(u'needs review'))
                    break
                elif t.type == Task.TYPE_IDS['Approve']:
                    tags.append(ugettext(u'needs approval'))
                    break
                else:
                    # subtitles are complete, but there's a subtitle/translate
                    # task for them.  They must have gotten sent back.
                    tags.append(ugettext(u'needs editing'))
                    break
        return tags

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)


def index(request):
    context = widget.add_onsite_js_files({})
    context['all_videos'] = Video.objects.count()
    context['popular_videos'] = VideoIndex.get_popular_videos("-today_views")[:VideoIndex.IN_ROW]
    context['featured_videos'] = VideoIndex.get_featured_videos()[:VideoIndex.IN_ROW]
    return render_to_response('index.html', context,
                              context_instance=RequestContext(request))

def watch_page(request):

    # Assume we're currently indexing if the number of public
    # indexed vids differs from the count of video objects by
    # more than 1000
    is_indexing = cache.get('is_indexing')
    if is_indexing is None:
        is_indexing = Video.objects.all().count() - VideoIndex.public().count() > 1000
        cache.set('is_indexing', is_indexing, 300)

    context = {
        'featured_videos': VideoIndex.get_featured_videos()[:VideoIndex.IN_ROW],
        'popular_videos': VideoIndex.get_popular_videos()[:VideoIndex.IN_ROW],
        'latest_videos': VideoIndex.get_latest_videos()[:VideoIndex.IN_ROW*3],
        'popular_display_views': 'week',
        'is_indexing': is_indexing
    }
    return render_to_response('videos/watch.html', context,
                              context_instance=RequestContext(request))

def featured_videos(request):
    return render_to_response('videos/featured_videos.html', {},
                              context_instance=RequestContext(request))

def latest_videos(request):
    return render_to_response('videos/latest_videos.html', {},
                              context_instance=RequestContext(request))

def popular_videos(request):
    return render_to_response('videos/popular_videos.html', {},
                              context_instance=RequestContext(request))

def volunteer_page(request):
    # Get the user comfort languages list
    user_langs = get_user_languages_from_request(request)

    relevant = VideoIndex.public().filter(video_language_exact__in=user_langs) \
        .filter_or(languages_exact__in=user_langs) \
        .order_by('-requests_count')

    featured_videos =  relevant.filter(
        featured__gt=datetime.datetime(datetime.MINYEAR, 1, 1)) \
        .order_by('-featured')[:5]

    popular_videos = relevant.order_by('-week_views')[:5]

    latest_videos = relevant.order_by('-edited')[:15]

    requested_videos = relevant.filter(requests_exact__in=user_langs)[:5]

    context = {
        'featured_videos': featured_videos,
        'popular_videos': popular_videos,
        'latest_videos': latest_videos,
        'requested_videos': requested_videos,
        'user_langs':user_langs,
    }

    return render_to_response('videos/volunteer.html', context,
                              context_instance=RequestContext(request))

def volunteer_category(request, category):
    '''
    Display results only for a particular category of video results from
    popular, featured and latest videos.
    '''
    return render_to_response('videos/volunteer_%s.html' %(category),
                              context_instance=RequestContext(request))


def create(request):
    video_form = VideoForm(request.user, request.POST or None)
    context = {
        'video_form': video_form,
        'youtube_form': AddFromFeedForm(request.user)
    }
    if video_form.is_valid():
        try:
            video = video_form.save()
        except (VidscraperError, RequestError):
            context['vidscraper_error'] = True
            return render_to_response('videos/create.html', context,
                          context_instance=RequestContext(request))
        messages.info(request, message=_(u'''Here is the subtitle workspace for your video. You can
share the video with friends, or get an embed code for your site.  To add or
improve subtitles, click the button below the video.'''))

        url_obj = video.videourl_set.filter(primary=True).all()[:1].get()
        if url_obj.type != 'Y':
            # Check for all types except for Youtube
            if not url_obj.effective_url.startswith('https'):
                messages.warning(request, message=_(u'''You have submitted a video
                that is served over http.  Your browser may display mixed
                content warnings.'''))

        if video_form.created:
            messages.info(request, message=_(u'''Existing subtitles will be imported in a few minutes.'''))
        return redirect(video.get_absolute_url())
    return render_to_response('videos/create.html', context,
                              context_instance=RequestContext(request))

create.csrf_exempt = True

def create_from_feed(request):
    form = AddFromFeedForm(request.user, request.POST or None)
    if form.is_valid():
        form.save()
        messages.success(request, form.success_message())
        return redirect('videos:create')
    context = {
        'video_form': VideoForm(),
        'youtube_form': form,
        'from_feed': True
    }
    return render_to_response('videos/create.html', context,
                              context_instance=RequestContext(request))

create_from_feed.csrf_exempt = True

def shortlink(request, encoded_pk):
    pk = base62.to_decimal(encoded_pk)
    video = get_object_or_404(Video, pk=pk)
    return redirect(video, video=video, permanent=True)

class VideoPageContext(dict):
    """Context dict for the video page."""
    def __init__(self, request, video, video_url, tab, tab_only=False):
        dict.__init__(self)
        self['video'] = video
        if not tab_only:
            video.prefetch_languages(with_public_tips=True,
                                     with_private_tips=True)
            self.setup(request, video, video_url)
        self.setup_tab(request, video, video_url, tab)

    def setup(self, request, video, video_url):
        language_for_locale = video.subtitle_language(request.LANGUAGE_CODE)
        if language_for_locale:
            metadata = language_for_locale.get_metadata()
        else:
            metadata = video.get_metadata()

        self.update(widget.add_onsite_js_files({}))
        self['page_title'] = self.page_title(video)
        self['metadata'] = metadata.convert_for_display()
        self['language_list'] = LanguageList(video)
        self['shows_widget_sharing'] = video.can_user_see(request.user)

        _add_share_panel_context_for_video(self, video)
        self['task'] =  _get_related_task(request)
        if video.get_team_video() is not None:
            self['team'] = video.get_team_video().team
        else:
            self['team'] = None

    def page_title(self, video):
        template = string.Template(ugettext("$title with subtitles | Amara"))
        return template.substitute(title=video.title_display())


    def setup_tab(self, request, video, video_url, tab):
        setup_tab_method = getattr(self, 'setup_tab_%s' % tab, None)
        if setup_tab_method is not None:
            setup_tab_method(request, video, video_url, tab)

    def setup_tab_video(self, request, video, video_url, tab):
        self['widget_params'] = _widget_params(
            request, video, language=None,
            video_url=video_url and video_url.effective_url,
            size=(620,370)
        )

@get_video_from_code
def redirect_to_video(request, video):
    return redirect(video, permanent=True)

@get_video_from_code
def video(request, video, video_url=None, title=None):
    """
    If user is about to perform a task on this video, then t=[task.pk]
    will be passed to as a url parameter.
    """

    if video_url:
        video_url = get_object_or_404(VideoUrl, pk=video_url)

    # FIXME: what is this crazy mess?
    if not video_url and ((video.title_for_url() and not video.title_for_url() == title) or (not video.title and title)):
        return redirect(video, permanent=True)

    video.update_view_counter()

    tab = request.GET.get('tab')
    if tab not in ('urls', 'comments', 'activity', 'video'):
        # force tab to be video if it doesn't match either of the other
        # tabs
        tab = 'video'

    if request.is_ajax():
        context = VideoPageContext(request, video, video_url, tab,
                                   tab_only=True)
        template_name = 'videos/video-%s-tab.html' % tab
    else:
        template_name = 'videos/video-%s.html' % tab
        context = VideoPageContext(request, video, video_url, tab)
        if 'tab' in request.GET:
            # we only want to update the view counter if this request wasn't
            # the result of a tab click.
            video.update_view_counter()
    context['tab'] = tab

    return render(request, template_name, context)

def _get_related_task(request):
    """
    Checks if request has t=[task-id], and if so checks if the current
    user can perform it, in case all goes well, return the task to be
    performed.
    """
    task_pk = request.GET.get('t', None)
    if task_pk:
        from teams.permissions import can_perform_task
        try:
            task = Task.objects.get(pk=task_pk)
            if can_perform_task(request.user, task):
                return task
        except Task.DoesNotExist:
            return


def actions_list(request, video_id):
    video = get_object_or_404(Video, video_id=video_id)
    qs = Action.objects.for_video(video)

    extra_context = {
        'video': video
    }

    return object_list(request, queryset=qs, allow_empty=True,
                       paginate_by=settings.ACTIVITIES_ONPAGE,
                       template_name='videos/actions_list.html',
                       template_object_name='action',
                       extra_context=extra_context)

@login_required
def upload_subtitles(request):
    output = {'success': False}
    video = Video.objects.get(id=request.POST['video'])
    form = SubtitlesUploadForm(request.user, video, True, request.POST,
                               request.FILES, initial={'primary_audio_language_code':video.primary_audio_language_code})

    response = lambda s: HttpResponse('<textarea>%s</textarea>' % json.dumps(s))

    try:
        if form.is_valid():
            version = form.save()
            output['success'] = True
            output['next'] = version.subtitle_language.get_absolute_url()
            output['msg'] = ugettext(
                u'Thank you for uploading. '
                u'It may take a minute or so for your subtitles to appear.')
        else:
            output['errors'] = form.get_errors()
    except AlreadyEditingException, e:
        output['errors'] = {'__all__': [force_unicode(e.msg)]}
    except Exception, e:
        import traceback
        traceback.print_exc()
        from raven.contrib.django.models import client
        client.create_from_exception()
        output['errors'] = {'__all__': [force_unicode(e)]}

    return response(output)

@login_required
def upload_transcription_file(request):
    output = {}
    form = TranscriptionFileForm(request.POST, request.FILES)
    if form.is_valid():
        output['text'] = getattr(form, 'file_text', '')
    else:
        output['errors'] = form.get_errors()
    return HttpResponse(u'<textarea>%s</textarea>'  % json.dumps(output))

def feedback(request, hide_captcha=False):
    output = dict(success=False)
    form = FeedbackForm(request.POST, initial={'captcha': request.META['REMOTE_ADDR']},
                        hide_captcha=hide_captcha)
    if form.is_valid():
        form.send(request)
        output['success'] = True
    else:
        output['errors'] = form.get_errors()
    return HttpResponse(json.dumps(output), "text/javascript")

def email_friend(request):
    text = request.GET.get('text', '')
    link = request.GET.get('link', '')
    if link:
        text = link if not text else '%s\n%s' % (text, link)
    from_email = ''
    if request.user.is_authenticated():
        from_email = request.user.email
    initial = dict(message=text, from_email=from_email)
    if request.method == 'POST':
        form = EmailFriendForm(request.POST, auto_id="email_friend_id_%s", label_suffix="")
        if form.is_valid():
            email_st = EmailShareStatistic()
            if request.user.is_authenticated():
                email_st.user = request.user
            email_st.save()

            form.send()
            messages.info(request, 'Email Sent!')

            return redirect(request.get_full_path())
    else:
        form = EmailFriendForm(auto_id="email_friend_id_%s", initial=initial, label_suffix="")
    context = {
        'form': form
    }
    return render_to_response('videos/email_friend.html', context,
                              context_instance=RequestContext(request))


@get_video_from_code
def legacy_history(request, video, lang=None):
    """
    In the old days we allowed only one translation per video.
    Therefore video urls looked like /vfjdh2/en/
    Now that this constraint is removed we need to redirect old urls
    to the new view, that needs
    """
    try:
        language = video.subtitle_language(lang)
        if language is None:
            raise SubtitleLanguage.DoesNotExist("No such language")
    except sub_models.SubtitleLanguage.DoesNotExist:
        raise Http404()

    return HttpResponseRedirect(reverse("videos:translation_history", kwargs={
            'video_id': video.video_id,
            'lang_id': language.pk,
            'lang': language.language_code,
            }))

class LanguagePageContext(dict):
    """Context dict for language pages

    This class defines the base class that sets up the variables we use for
    all the languages classes.  For the specific language pages (subtitles,
    comments, revisions), we use a subclass of this.
    """
    def __init__(self, request, video, lang_code, lang_id, version_id,
                 tab_only=False):
        dict.__init__(self)
        self.public_only = self.calc_public_only(request, video)
        language = self._get_language(video, lang_code, lang_id)
        version = self._get_version(request, video, language, version_id)
        self['video'] = video
        self['language'] = language
        self['version'] = version
        if not tab_only:
            video.prefetch_languages(with_public_tips=True,
                                     with_private_tips=True)
            self.setup(request, video, language, version)
        self.setup_tab(request, video, language, version)

    def _get_language(self, video, lang_code, lang_id):
        """Get a language for the language page views.

        For historical reasons, we normally specify both a language code and a
        language id.  This method takes both of those and returns a
        SubtitleLanguage.
        """
        try:
            language = video.language_with_pk(lang_id)
        except SubtitleLanguage.DoesNotExist:
            raise Http404
        if language.language_code != lang_code:
            raise Http404
        return language

    def calc_public_only(self, request, video):
        team_video = video.get_team_video()
        return (team_video and not team_video.team.is_member(request.user))

    def _get_version(self, request, video, language, version_id):
        """Get the SubtitleVersion to use for a language page."""
        team_video = video.get_team_video()
        if version_id:
            try:
                return language.get_version_by_id(version_id,
                                                  public=self.public_only)
            except sub_models.SubtitleVersion.DoesNotExist:
                raise Http404
        else:
            return language.get_tip(public=self.public_only)

    def setup(self, request, video, language, version):
        """Setup context variables."""

        self.update(widget.add_onsite_js_files({}))

        self['revision_count'] = language.version_count()
        self['language_list'] = LanguageList(video)
        self['page_title'] = self.page_title(language)
        self['edit_url'] = language.get_widget_url()
        self['shows_widget_sharing'] = video.can_user_see(request.user)
        self['widget_params'] = _widget_params(request, video, version_no=None,
                                                  language=language,
                                                  size=(289, 173))
        _add_share_panel_context_for_history(self, video, language)
        if version is not None:
            self['metadata'] = version.get_metadata().convert_for_display()
        else:
            self['metadata'] = video.get_metadata().convert_for_display()

        self['rollback_allowed'] = self.calc_rollback_allowed(
            request, version, language)

    def calc_rollback_allowed(self, request, version, language):
        if version and version.next_version():
            return (version.video.get_team_video() is None or
                    can_rollback_language(request.user, language))
        else:
            return False

    def setup_tab(self, request, video, language, video_url):
        """Setup tab-specific variables."""
        pass

    def page_title(self, language):
        template = string.Template(ugettext("$title with subtitles | Amara"))
        return template.substitute(title=language.title_display())

class LanguagePageContextSubtitles(LanguagePageContext):
    def setup_tab(self, request, video, language, version):
        team_video = video.get_team_video()
        user_can_add_version = can_add_version(request.user, video,
                                               language.language_code)

        self['downloadable_formats'] = AVAILABLE_SUBTITLE_FORMATS_FOR_DISPLAY
        self['edit_disabled'] = not user_can_add_version
        # If there are tasks for this language, the user has to go through the
        # tasks panel to edit things instead of doing it directly from here.
        if user_can_add_version and video.get_team_video():
            has_open_task = (Task.objects.incomplete()
                             .filter(team_video=video.get_team_video(),
                                     language=language.language_code)
                             .exists())
            if has_open_task:
                self['edit_disabled'] = True
                self['must_use_tasks'] = True
        if 'rollback_allowed' not in self:
            self['rollback_allowed'] = self.calc_rollback_allowed(
                request, version, language)

class LanguagePageContextComments(LanguagePageContext):
    pass

class LanguagePageContextRevisions(LanguagePageContext):
    REVISIONS_PER_PAGE = 10

    def setup_tab(self, request, video, language, version):
        if self.public_only:
            revisions_qs = language.subtitleversion_set.public()
        else:
            revisions_qs = language.subtitleversion_set.extant()
        revisions_qs = revisions_qs.order_by('-version_number')

        revisions, pagination_info = paginate(
            revisions_qs, self.REVISIONS_PER_PAGE, request.GET.get('page'))
        self.update(pagination_info)
        self['revisions'] = language.optimize_versions(revisions)

class LanguagePageContextSyncHistory(LanguagePageContext):
    def setup_tab(self, request, video, language, version):
        self['sync_history'] = language.synchistory_set.order_by('-id').all()
        self['current_version'] = language.get_public_tip()
        synced_versions = []
        for video_url in video.get_video_urls():
            if not can_sync_videourl(video_url):
                continue
            try:
                version = (language.syncedsubtitleversion_set.
                           select_related('version').
                           get(video_url=video_url)).version
            except ObjectDoesNotExist:
                version = None
            synced_versions.append({
                'video_url': video_url,
                'version': version,
            })
        self['synced_versions'] = synced_versions

@get_video_from_code
def language_subtitles(request, video, lang, lang_id, version_id=None):
    tab = request.GET.get('tab')
    if tab == 'revisions':
        ContextClass = LanguagePageContextRevisions
    elif tab == 'comments':
        ContextClass = LanguagePageContextComments
    elif tab == 'sync-history':
        if not request.user.is_staff:
            return redirect_to_login(request.build_absolute_uri())
        ContextClass = LanguagePageContextSyncHistory
    else:
        # force tab to be subtitles if it doesn't match either of the other
        # tabs
        tab = 'subtitles'
        ContextClass = LanguagePageContextSubtitles

    if request.is_ajax():
        context = ContextClass(request, video, lang, lang_id, version_id,
                               tab_only=True)
        template_name = 'videos/language-%s-tab.html' % tab
    else:
        template_name = 'videos/language-%s.html' % tab
        context = ContextClass(request, video, lang, lang_id, version_id)
        context['tab'] = tab
        if 'tab' not in request.GET:
            # we only want to update the view counter if this request wasn't
            # the result of a tab click.
            video.update_view_counter()
    return render(request, template_name, context)

def _widget_params(request, video, version_no=None, language=None, video_url=None, size=None):
    primary_url = video_url or video.get_video_url()
    alternate_urls = [vu.effective_url for vu in video.videourl_set.all()
                      if vu.effective_url != primary_url]
    params = {'video_url': primary_url,
              'alternate_video_urls': alternate_urls,
              'base_state': {}}

    if version_no:
        params['base_state']['revision'] = version_no

    if language:
        params['base_state']['language_code'] = language.language_code
        params['base_state']['language_pk'] = language.pk
    if size:
        params['video_config'] = {"width":size[0], "height":size[1]}

    return base_widget_params(request, params)

@login_required
@get_video_revision
def rollback(request, version):
    is_writelocked = version.subtitle_language.is_writelocked
    team_video = version.video.get_team_video()
    if team_video and not can_rollback_language(request.user,
                                                version.subtitle_language):
        messages.error(request, _(u"You don't have permission to rollback "
                                  "this language"))
    elif is_writelocked:
        messages.error(request, u'Can not rollback now, because someone is editing subtitles.')
    elif not version.next_version():
        messages.error(request, message=u'Can not rollback to the last version')
    else:
        messages.success(request, message=u'Rollback successful')
        version = rollback_to(version.video,
                version.subtitle_language.language_code,
                version_number=version.version_number,
                rollback_author=request.user)
        video_changed_tasks.delay(version.video.id, version.id)
        return redirect(version.subtitle_language.get_absolute_url()+'#revisions')
    return redirect(version)

@get_video_revision
def diffing(request, first_version, second_pk):
    language = first_version.subtitle_language
    second_version = get_object_or_404(
        sub_models.SubtitleVersion.objects.extant(),
        pk=second_pk, subtitle_language=language)

    if first_version.video != second_version.video:
        # this is either a bad bug, or someone evil
        raise "Revisions for diff videos"

    if first_version.pk < second_version.pk:
        # this is just stupid Instead of first, second meaning
        # chronological order (first cames before second)
        # it means  the opposite, so make sure the first version
        # has a larger version no than the second
        first_version, second_version = second_version, first_version

    video = first_version.subtitle_language.video
    diff_data = diff_subs(first_version.get_subtitles(), second_version.get_subtitles())
    team_video = video.get_team_video()

    context = widget.add_onsite_js_files({})
    context['video'] = video
    context['diff_data'] = diff_data
    context['language'] = language
    context['first_version'] = first_version
    context['second_version'] = second_version
    context['latest_version'] = language.get_tip()
    if team_video and not can_rollback_language(request.user, language):
        context['rollback_allowed'] = False
    else:
        context['rollback_allowed'] = True
    context['widget0_params'] = \
        _widget_params(request, video,
                       first_version.version_number)
    context['widget1_params'] = \
        _widget_params(request, video,
                       second_version.version_number)
    return render_to_response('videos/diffing.html', context,
                              context_instance=RequestContext(request))

def test_form_page(request):
    if request.method == 'POST':
        form = UserTestResultForm(request.POST)
        if form.is_valid():
            form.save(request)
            messages.success(request, 'Thanks for your feedback.  It\'s a huge help to us as we improve the site.')
            return redirect('videos:test_form_page')
    else:
        form = UserTestResultForm()
    context = {
        'form': form
    }
    return render_to_response('videos/test_form_page.html', context,
                              context_instance=RequestContext(request))

@login_required
def stop_notification(request, video_id):
    user_id = request.GET.get('u')
    hash = request.GET.get('h')

    if not user_id or not hash:
        raise Http404

    video = get_object_or_404(Video, video_id=video_id)
    user = get_object_or_404(User, id=user_id)
    context = dict(video=video, u=user)

    if hash and user.hash_for_video(video_id) == hash:
        video.followers.remove(user)
        for l in video.subtitlelanguage_set.all():
            l.followers.remove(user)
        if request.user.is_authenticated() and not request.user == user:
            logout(request)
    else:
        context['error'] = u'Incorrect secret hash'
    return render_to_response('videos/stop_notification.html', context,
                              context_instance=RequestContext(request))

@login_required
@require_POST
def video_url_make_primary(request):
    output = {}
    id = request.POST.get('id')
    status = 200
    if id:
        try:
            obj = VideoUrl.objects.get(id=id)
            tv = obj.video.get_team_video()
            if tv and not permissions.can_user_edit_video_urls(obj.video, request.user):
                output['error'] = ugettext('You have not permission change this URL')
                status = 403
            else:
                obj.make_primary(user=request.user)
        except VideoUrl.DoesNotExist:
            output['error'] = ugettext('Object does not exist')
            status = 404
    return HttpResponse(json.dumps(output), status=status)

@login_required
@require_POST
def video_url_remove(request):
    output = {}
    id = request.POST.get('id')
    status = 200
    if id:
        try:
            obj = VideoUrl.objects.get(id=id)
            tv = obj.video.get_team_video()
            if tv and not permissions.can_user_edit_video_urls(obj.video, request.user):
                output['error'] = ugettext('You have not permission delete this URL')
                status = 403
            else:
                if obj.primary:
                    output['error'] = ugettext('You can\'t remove primary URL')
                    status = 403
                else:
                    # create activity record
                    act = Action(video=obj.video, action_type=Action.DELETE_URL)
                    act.new_video_title = obj.url
                    act.created = datetime.datetime.now()
                    act.user = request.user
                    act.save()
                    # delete
                    obj.delete()
        except VideoUrl.DoesNotExist:
            output['error'] = ugettext('Object does not exist')
            status = 404
    return HttpResponse(json.dumps(output), status=status)

@login_required
def video_url_create(request):
    output = {}

    form = CreateVideoUrlForm(request.user, request.POST)
    if form.is_valid():
        obj = form.save()
        video = form.cleaned_data['video']
        users = video.notification_list(request.user)

        for user in users:
            subject = u'New video URL added by %(username)s to "%(video_title)s" on universalsubtitles.org'
            subject = subject % {'url': obj.url, 'username': obj.added_by, 'video_title': video}
            context = {
                'video': video,
                'video_url': obj,
                'user': user,
                'domain': Site.objects.get_current().domain,
                'hash': user.hash_for_video(video.video_id)
            }
            Meter('templated-emails-sent-by-type.videos.video-url-added').inc()
            send_templated_email(user, subject,
                                 'videos/email_video_url_add.html',
                                 context, fail_silently=not settings.DEBUG)
    else:
        output['errors'] = form.get_errors()

    return HttpResponse(json.dumps(output))

@staff_member_required
def reindex_video(request, video_id):
    from teams.tasks import update_one_team_video

    video = get_object_or_404(Video, video_id=video_id)
    video.update_search_index()

    team_video = video.get_team_video()

    if team_video:
        update_one_team_video.delay(team_video.id)

def subscribe_to_updates(request):
    email_address = request.POST.get('email_address', '')
    data = urllib.urlencode({'email': email_address})
    req = urllib2.Request(
        'http://pcf8.pculture.org/interspire/form.php?form=3', data)
    urllib2.urlopen(req)
    return HttpResponse('ok', 'text/plain')

def test_celery(request):
    from videos.tasks import add
    add.delay(1, 2)
    return HttpResponse('Hello, from Amazon SQS backend for Celery!')

@staff_member_required
def test_celery_exception(request):
    from videos.tasks import raise_exception
    raise_exception.delay('Exception in Celery', should_be_logged='Hello, man!')
    return HttpResponse('Hello, from Amazon SQS backend for Celery! Look for exception.')

@never_in_prod
@staff_member_required
def video_staff_delete(request, video_id):
    video = get_object_or_404(Video, video_id=video_id)
    video.delete()
    return HttpResponse("ok")

def video_debug(request, video_id):
    from apps.widget import video_cache as vc
    from django.core.cache import cache
    from accountlinker.models import youtube_sync
    from videos.models import VIDEO_TYPE_YOUTUBE

    video = get_object_or_404(Video, video_id=video_id)
    vid = video.video_id
    get_subtitles_dict = {}

    for l in video.newsubtitlelanguage_set.all():
        cache_key = vc._subtitles_dict_key(vid, l.pk, None)
        get_subtitles_dict[l.language_code] = cache.get(cache_key)

    cache = {
        "get_video_urls": cache.get(vc._video_urls_key(vid)),
        "get_subtitles_dict": get_subtitles_dict,
        "get_video_languages": cache.get(vc._video_languages_key(vid)),

        "get_video_languages_verbose": cache.get(vc._video_languages_verbose_key(vid)),
        "writelocked_langs": cache.get(vc._video_writelocked_langs_key(vid)),
    }

    tasks = Task.objects.filter(team_video=video)

    is_youtube = video.videourl_set.filter(type=VIDEO_TYPE_YOUTUBE).count() != 0

    if request.method == 'POST' and request.POST.get('action') == 'sync':
        # Sync video to youtube
        sync_lang = sub_models.SubtitleLanguage.objects.get(
                pk=request.POST.get('language'))
        youtube_sync(video, sync_lang)

    return render_to_response("videos/video_debug.html", {
            'video': video,
            'is_youtube': is_youtube,
            'tasks': tasks,
            "cache": cache
    }, context_instance=RequestContext(request))

def reset_metadata(request, video_id):
    video = get_object_or_404(Video, video_id=video_id)
    video_changed_tasks.delay(video.id)
    return HttpResponse('ok')

def set_original_language(request, video_id):
    """
    We only allow if a video is own a team, or the video owner is the
    logged in user
    """
    video = get_object_or_404(Video, video_id=video_id)
    if not (can_edit_video(video.get_team_video(), request.user) or video.user == request.user):
        return HttpResponseForbidden("Can't touch this.")
    form = ChangeVideoOriginalLanguageForm(request.POST or None, initial={
        'language_code': video.primary_audio_language_code
    })
    if request.method == "POST" and form.is_valid():
        video.primary_audio_language_code = form.cleaned_data['language_code']
        video.save()
        messages.success(request, _(u'The language for %s has been changed' % (video)))
        return HttpResponseRedirect(reverse("videos:set_original_language", args=(video_id,)))
    return render_to_response("videos/set-original-language.html", {
        "video": video,
        'form': form
    }, context_instance=RequestContext(request)
    )
