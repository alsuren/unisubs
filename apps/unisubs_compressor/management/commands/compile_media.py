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

import sys, os, shutil, subprocess, logging, time
import re

from django.conf import settings
from django.core.management.base import BaseCommand
from django.contrib import admin
from django.contrib.sites.models import Site
from django.template.loader import render_to_string

import optparse

from deploy.git_helpers import get_current_commit_hash

from apps import widget
from apps.unisubs_compressor.contrib.rjsmin import jsmin

# on vagrant .git is a symlink and this needts to be ran before media compilation ;(
LAST_COMMIT_GUID = get_current_commit_hash() or settings.LAST_COMMIT_GUID.split('/')[-1]

def _make_version_debug_string():
    """
    See Command._append_verion_for_debug

    We have this as an external function because we need this on compilation and testing deployment
    """
    return '/*unisubs.static_version="%s"*/' % LAST_COMMIT_GUID




logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')

def to_static_root(*path_components):
    return os.path.join(settings.STATIC_ROOT, *path_components)
JS_LIB = os.path.join(settings.PROJECT_ROOT, "media")
CLOSURE_LIB = os.path.join(JS_LIB, "js", "closure-library")
FLOWPLAYER_JS = os.path.join(
    settings.PROJECT_ROOT, "media/flowplayer/flowplayer-3.2.6.min.js")
COMPILER_PATH = os.path.join(settings.PROJECT_ROOT,  "closure", "compiler.jar")


DIRS_TO_COMPILE = []
SKIP_COPING_ON = DIRS_TO_COMPILE + [
    "videos",
    "*closure-lib*" ,
    settings.COMPRESS_OUTPUT_DIRNAME,
    "teams",
     ]

NO_UNIQUE_URL = (
# TODO: Figure out if you can uncomment this, then possibly remove
# special case for it in send_to_s3
#    {
#        "name": "embed.js",
#        "no-cache": True
#    },
    {
        "name": "images/video-no-thumbnail-medium.png",
        "no-cache": True,
    },
    {
        "name": "images/video-no-thumbnail-small.png",
        "no-cache": True,
    },
    {
        "name": "js/unisubs-widgetizer.js",
        "no-cache": True
    }, {
        "name": "js/unisubs-widgetizer-debug.js",
        "no-cache": True,
    }, {
        "name": "js/unisubs-widgetizer-sumo.js",
        "no-cache": True,
    }, {
        "name": "js/unisubs-api.js",
        "no-cache": True
    }, {
        "name": "js/unisubs-statwidget.js",
        "no-cache": False,
    }, {
        "name": "js/widgetizer/widgetizerprimer.js",
        "no-cache": True
    },{
        "name": "release/public/embedder.js",
        "no-cache": True
    },{
        "name": "release/public/embedder.css",
        "no-cache": True
    }


)

def call_command(command):
    process = subprocess.Popen(command.split(' '),
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    return process.communicate()

def get_cache_base_url():
    return "%s%s/%s" % (settings.STATIC_URL_BASE, settings.COMPRESS_OUTPUT_DIRNAME, LAST_COMMIT_GUID)

def get_cache_dir():
    # on vagrant this is a symlink
    return os.path.realpath(
        os.path.join(
            settings.STATIC_ROOT,
            settings.COMPRESS_OUTPUT_DIRNAME, LAST_COMMIT_GUID))

def sorted_ls(path):
    """
    Returns contents of dir from older to newer
    """
    mtime = lambda f: os.stat(os.path.join(path, f)).st_mtime
    return list(sorted(os.listdir(path), key=mtime))

class Command(BaseCommand):
    """

    """


    help = 'Compiles all bundles in settings.py (css and js).'
    args = 'media_bundles'

    option_list = BaseCommand.option_list + (

        optparse.make_option('--checks-version',
            action='store_true', dest='test_str_version', default=False,
            help="Check that we outputed the version string for comopiled files."),
        optparse.make_option('--keeps-previous',
            action='store_true', dest='keeps_previous', default=False,
            help="Will remove older static media builds."),
        optparse.make_option('--compilation-level',
            action='store', dest='compilation_level', default='ADVANCED_OPTIMIZATIONS',
            help="How aggressive is compilation. Possible values: ADVANCED_OPTIMIZATIONS, WHITESPACE_ONLY and SIMPLE_OPTIMIZATIONS"),
        )

    def _append_version_for_debug(self, descriptor, file_type):
        """
        We append the /*unisubs.static_version="{{commit guid}"*/ to the end of the
        file so we can debug, be sure we have the correct version of media.

        Arguments:
        `descriptor` : the fd to append to
        `file_type` : if it's a js or html or css file - we currently only support js and css
            """
        descriptor.write(_make_version_debug_string())

    def compile_css_bundle(self, bundle_name, files):
        bundle_settings = settings.MEDIA_BUNDLES[bundle_name]
        file_list = [os.path.join(settings.STATIC_ROOT, x) for x in files]
        for f in file_list:
            open(f).read()
        buffer = [open(f).read() for f in file_list]
        
        if 'output' in bundle_settings:
            concatenated_path =  os.path.join(self.temp_dir, bundle_settings['output'])
            dir_path = os.path.dirname(concatenated_path)
        else:
            dir_path = os.path.join(self.temp_dir, "css-compressed")
            concatenated_path =  os.path.join(dir_path,
                                              "%s.css" % bundle_name)
        if os.path.exists(dir_path) is False:
            os.makedirs(dir_path)
        out = open(concatenated_path, 'w')
        out.write("".join(buffer))
        out.close()
        filename = "%s.css" % ( bundle_name)
        cmd_str = "%s --type=css %s" % (settings.COMPRESS_YUI_BINARY, concatenated_path)
        if self.verbosity > 1:
            logging.info( "calling %s" % cmd_str)
        output, err_data  = call_command(cmd_str)


        out = open(concatenated_path, 'w')
        out.write(output)
        self._append_version_for_debug(out, "css")
        out.close()
        #os.remove(concatenated_path)
        return  filename

    def compile_js_bundle(self, bundle_name, files):
        self.ensure_js_dir_exists()
        bundle_settings = settings.MEDIA_BUNDLES[bundle_name]
        if bundle_settings.get('use_closure'):
            return self.compile_js_closure_bundle(bundle_name, files)

        output_file_name = bundle_name + '.js'
        output_path = os.path.join(self.temp_dir, "js" , output_file_name)
        with open(output_path, 'w') as output:
            for input_filename in files:
                input_path = to_static_root(input_filename)
                minified = jsmin(open(input_path).read());
                output.write('/* %s */\n' % input_filename);
                output.write("%s;\n" % (minified,))

    def ensure_js_dir_exists(self):
        temp_js_dir = os.path.join(self.temp_dir, 'js')
        if not os.path.exists(temp_js_dir):
            os.makedirs(temp_js_dir)

    def compile_js_closure_bundle(self, bundle_name, files):
        bundle_settings = settings.MEDIA_BUNDLES[bundle_name]
        if 'bootloader' in bundle_settings:
            output_file_name = "{0}-inner.js".format(bundle_name)
        else:
            output_file_name = "{0}.js".format(bundle_name)

        debug = bundle_settings.get("debug", False)
        extra_defines = bundle_settings.get("extra_defines", None)
        include_flash_deps = bundle_settings.get("include_flash_deps", True)
        closure_dep_file = bundle_settings.get("closure_deps",'js/closure-dependencies.js' )
        optimization_type = bundle_settings.get("optimizations", self.compilation_level)

        logging.info("Starting {0}".format(output_file_name))

        deps = [" --js %s " % os.path.join(JS_LIB, file) for file in files]
        if 'output' in bundle_settings:
            if 'bootloader' in bundle_settings:
                name = bundle_settings['output']
                name = "".join([os.path.splitext(name)[0], '-inner', os.path.splitext(name)[1]])
            compiled_js = os.path.join(self.temp_dir, name)
        else:
            compiled_js = os.path.join(self.temp_dir, "js" , output_file_name)
        compiler_jar = COMPILER_PATH

        logging.info("Calculating closure dependencies")

        js_debug_dep_file = ''
        if debug:
            js_debug_dep_file = '-i {0}/{1}'.format(JS_LIB, 'js/closure-debug-dependencies.js')

        cmd_str = "%s/closure/bin/calcdeps.py -i %s/%s %s -p %s/ -o script"  % (
            CLOSURE_LIB,
            JS_LIB,
            closure_dep_file,
            js_debug_dep_file,
            CLOSURE_LIB)
        if self.verbosity > 1:
            logging.info( "calling %s" % cmd_str)
        output,_ = call_command(cmd_str)

        # This is to reduce the number of warnings in the code.
        # The unisubs-calcdeps.js file is a concatenation of a bunch of Google Closure
        # JavaScript files, each of which has a @fileoverview tag to describe it.
        # When put all in one file, the compiler complains, so remove them all.
        output_lines = filter(lambda s: s.find("@fileoverview") == -1,
                              output.split("\n"))

        calcdeps_js = os.path.join(JS_LIB, 'js', 'unisubs-calcdeps.js')
        calcdeps_file = open(calcdeps_js, "w")
        if 'ignore_closure' in bundle_settings:
            calcdeps_file.write("\n")
        else:
            calcdeps_file.write("\n".join(output_lines))
        calcdeps_file.close()

        logging.info("Compiling {0}".format(output_file_name))

        debug_arg = ''
        if not debug:
            debug_arg = '--define goog.DEBUG=false'
        extra_defines_arg = ''
        if extra_defines is not None:
            for k, v in extra_defines.items():
                extra_defines_arg += ' --define {0}={1} '.format(k, v)
        cmd_str =  ("java -jar %s --js %s %s --js_output_file %s %s %s "
                    "--define goog.NATIVE_ARRAY_PROTOTYPES=false "
                    "--output_wrapper (function(){%%output%%})(); "
                    "--warning_level QUIET "
                    "--compilation_level %s") % \
                    (compiler_jar, calcdeps_js, deps, compiled_js,
                     debug_arg, extra_defines_arg, optimization_type)

        if self.verbosity > 1:
            logging.info( "calling %s" % cmd_str)
        output,err = call_command(cmd_str)
        if err:
            # if an error comes up, is will look like:
            sys.stderr.write("Error compiling : %s \n%s" % (bundle_name, err))

        with open(compiled_js, 'r') as compiled_js_file:
            compiled_js_text = compiled_js_file.read()

        with open(compiled_js, 'w') as compiled_js_file:

            # Include dependencies needed for DFXP parsing.
            with open(os.path.join(JS_LIB, 'src', 'js', 'third-party', 'amara-jquery.min.js'), 'r') as jqueryjs_file:
                compiled_js_file.write(jqueryjs_file.read())
            with open(os.path.join(JS_LIB, 'src', 'js', 'dfxp', 'dfxp.js'), 'r') as dfxpjs_file:
                compiled_js_file.write(dfxpjs_file.read())

            if include_flash_deps:
                with open(os.path.join(JS_LIB, 'js', 'swfobject.js'), 'r') as swfobject_file:
                    compiled_js_file.write(swfobject_file.read())
                with open(FLOWPLAYER_JS, 'r') as flowplayerjs_file:
                    compiled_js_file.write(flowplayerjs_file.read())
            compiled_js_file.write(compiled_js_text)
            self._append_version_for_debug(compiled_js_file, "js")
        if len(output) > 0:
            logging.info("compiler.jar output: %s" % output)

        if 'bootloader' in bundle_settings:
            self._compile_js_bootloader(
                bundle_name, bundle_settings['bootloader'])

        if len(err) > 0:
            logging.info("stderr: %s" % err)
        else:
            logging.info("Successfully compiled {0}".format(output_file_name))

    def _compile_js_bootloader(self, bundle_name, bootloader_settings):
        bundle_settings = settings.MEDIA_BUNDLES[bundle_name]
        logging.info("_compile_js_bootloader called with cache_base_url {0}".format(
                get_cache_base_url()))
        context = { 'gatekeeper' : bootloader_settings['gatekeeper'],
                    'script_src': "{0}/js/{1}-inner.js".format(
                get_cache_base_url(), bundle_name) }
        template_name = "widget/bootloader.js"
        if "template" in bootloader_settings:
            template_name = bootloader_settings["template"]
        rendered = render_to_string(template_name, context)
        file_name = os.path.join(
            self.temp_dir, "js", "{0}.js".format(bundle_name))
        output_override = bundle_settings.get('output', None)
        if output_override:
            file_name = os.path.join(self.temp_dir, output_override)
        uncompiled_file_name = os.path.join(
                self.temp_dir, "js", "{0}-uncompiled.js".format(bundle_name))
        with open(uncompiled_file_name, 'w') as f:
            f.write(rendered)
        cmd_str = ("java -jar {0} --js {1} --js_output_file {2} "
                   "--compilation_level {3}").format(
            COMPILER_PATH, uncompiled_file_name, file_name, self.compilation_level)
        call_command(cmd_str)
        os.remove(uncompiled_file_name)

    def compile_media_bundle(self, bundle_name, bundle_type, files):
        getattr(self, "compile_%s_bundle" % bundle_type)(bundle_name, files)

    def _create_temp_dir(self):
        commit_hash = LAST_COMMIT_GUID
        temp = os.path.join("/tmp", "static-%s-%s" % (commit_hash, time.time()))
        os.makedirs(temp)
        return temp

    def _copy_static_root_to_temp_dir(self):
        mr = settings.STATIC_ROOT
        for dirname in os.listdir(mr):
            original_path = os.path.join(mr, dirname)
            if os.path.isdir(original_path) and dirname not in SKIP_COPING_ON :
                dest =  os.path.join(self.temp_dir, dirname)
                if os.path.exists(dest):
                    shutil.rmtree(dest)
                shutil.copytree(original_path,
                         dest,
                         ignore=shutil.ignore_patterns(*SKIP_COPING_ON))

    def _copy_admin_media_to_cache_dir(self):
        # temporary until we switch to staticfiles
        # find admin media
        admin_media_dir = os.path.join(os.path.dirname(admin.__file__), 'static')
        for dirname in os.listdir(admin_media_dir):
            original_path = os.path.join(admin_media_dir, dirname)
            if os.path.isdir(original_path) and dirname not in SKIP_COPING_ON :
                dest =  os.path.join(self.temp_dir, dirname)
                if os.path.exists(dest):
                    shutil.rmtree(dest)
                shutil.copytree(original_path,
                         dest,
                         ignore=shutil.ignore_patterns(*SKIP_COPING_ON))

    def _copy_integration_root_to_temp_dir(self):
        """
        We 'merge' whatever is on unisubs-integration/media to
        project-root/media. This allows partners to have their own
        media compiled for deployment.
        Also see how unisusb-integration/integration_settings.py
        injects the dependencies for media compilation.
        """
        mr = os.path.join(settings.INTEGRATION_PATH , "media")
        for (dirpath, dirnames, filenames) in os.walk(mr):
            for file_name in filenames:
                original_path = os.path.join(dirpath, file_name)
                offset_path = original_path[len(mr) + 1:]
                final_path = os.path.join(settings.STATIC_ROOT, offset_path)
                final_dir = os.path.dirname(final_path)
                if os.path.exists(final_dir) is False:
                    os.makedirs(final_dir)
                if os.path.exists(final_path):
                    os.remove(final_path)
                try:
                    shutil.copy(original_path, final_path)
                except shutil.Error:
                    # if the files haven't changed this will be raised
                    pass

    def _output_embed_to_dir(self, output_dir, version=''):
        file_name = 'embed{0}.js'.format(version)
        context = widget.add_offsite_js_files(
            {'current_site': Site.objects.get_current(),
             'STATIC_URL': get_cache_base_url() +"/",
             'COMPRESS_MEDIA': settings.COMPRESS_MEDIA,
             "js_file": get_cache_base_url() +"/js/unisubs-offsite-compiled.js" })
        rendered = render_to_string(
            'widget/{0}'.format(file_name), context)
        with open(os.path.join(output_dir, file_name), 'w') as f:
            f.write(rendered)

    def _compile_conf_and_embed_js(self):
        """
        Compiles config.js, statwidgetconfig.js, and embed.js. These
        are used to provide build-specific info (like media url and site url)
        to compiled js.
        """
        logging.info(("_compile_conf_and_embed_js with cache_base_url {0}").format(
                get_cache_base_url()))

        file_name = os.path.join(JS_LIB, 'js/config.js')

        context = {'current_site': Site.objects.get_current(),
                   'STATIC_URL': get_cache_base_url()+ "/",
                   'COMPRESS_MEDIA': settings.COMPRESS_MEDIA }
        rendered = render_to_string(
            'widget/config.js', context)
        with open(file_name, 'w') as f:
            f.write(rendered)
        logging.info("Compiled config to %s" % (file_name))
        self._output_embed_to_dir(settings.STATIC_ROOT)
        self._output_embed_to_dir(
            settings.STATIC_ROOT, settings.EMBED_JS_VERSION)
        for version in settings.PREVIOUS_EMBED_JS_VERSIONS:
            self._output_embed_to_dir(settings.STATIC_ROOT, version)

        file_name = os.path.join(JS_LIB, 'js/statwidget/statwidgetconfig.js')
        rendered = render_to_string(
            'widget/statwidgetconfig.js', context)
        with open(file_name, 'w') as f:
            f.write(rendered)

        # these are the configs for the embedder
        file_name = os.path.join(JS_LIB, 'src/js/embedder/conf.js')
        rendered = render_to_string(
            'embedder/conf.js', context)
        with open(file_name, 'w') as f:
            f.write(rendered)

    def _compile_media_bundles(self, restrict_bundles, args):
        bundles = settings.MEDIA_BUNDLES
        for bundle_name, data in bundles.items():
            if restrict_bundles and bundle_name not in args:
                continue
            print "compiling %s: %s" % (data['type'], bundle_name)
            self.compile_media_bundle(
                bundle_name, data['type'], data["files"])

    def _remove_cache_dirs_before(self, num_to_keep):
        """
        we remove all but the last export, since the build can fail at the next step
        in which case it will still need the previous build there
        """
        base = os.path.dirname(get_cache_dir())
        if not os.path.exists(os.path.join(os.getcwd(), "media/static-cache")):
            return
        targets = [os.path.join(base, x) for x
                   in sorted_ls("media/static-cache/")
                   if x.startswith(".") is False and x != LAST_COMMIT_GUID ][:-num_to_keep]
        [shutil.rmtree(os.path.realpath(t)) for t in targets if os.path.exists(t)]

    def _copy_temp_dir_to_cache_dir(self):
        cache_dir = get_cache_dir()

        assert not os.path.islink(cache_dir), (
            "ERROR: %s is a symlink.  It must be a normal directory to compile static media." % cache_dir
        )

        if os.path.exists(cache_dir):
            shutil.rmtree(cache_dir)
        for filename in os.listdir(self.temp_dir):
            from_path = os.path.join(self.temp_dir, filename)
            to_path = os.path.join(cache_dir, filename)
            shutil.move(from_path,  to_path)


    def _copy_files_with_public_urls_from_cache_dir_to_static_dir(self):
        cache_dir = get_cache_dir()
        to_move = NO_UNIQUE_URL + ({'name': 'js/embedder.js', 'no-cache': False, 'output': 'release/public/embedder.js'},)
        for file in to_move:
            filename = file['name']
            from_path = os.path.join(cache_dir, filename)
            to_path =  os.path.join(settings.STATIC_ROOT, file.get('output', filename))
            if not os.path.exists(from_path):
                continue
            if os.path.exists(to_path):
                os.remove(to_path)
            if not os.path.exists(os.path.dirname(to_path)):
                os.makedirs(os.path.dirname(to_path))
            shutil.copyfile(from_path, to_path)

    def _make_mirosubs_copies_of_files_with_public_urls(self):
        # for backwards compatibilty with old mirosubs names
        for file in NO_UNIQUE_URL:
            filename = file['name']
            mirosubs_filename = re.sub(
                r'unisubs\-', 'mirosubs-',
                filename)
            if filename != mirosubs_filename:
                from_path = os.path.join(settings.STATIC_ROOT, filename)
                to_path = os.path.join(settings.STATIC_ROOT, mirosubs_filename)
                shutil.copyfile(from_path, to_path)

    def handle(self, *args, **options):
        """
        There are three directories involved here:

        temp_dir: /tmp/static-[commit guid]-[time] This is the working dir
            for the compilation.
        MEDIA_ROOT: regular media root directory for django project
        cache_dir: STATIC_ROOT/static-cache/[commit guid] where compiled
            media ends up
        """
        self.temp_dir = self._create_temp_dir()
        logging.info(("Starting static media compilation with "
                      "temp_dir {0} and cache_dir {1}").format(
                self.temp_dir, get_cache_dir()));
        self.verbosity = int(options.get('verbosity'))
        self.test_str_version = bool(options.get('test_str_version'))
        self.keeps_previous = bool(options.get('keeps_previous'))
        self.compilation_level = options.get('compilation_level')
        restrict_bundles = bool(args)

        os.chdir(settings.PROJECT_ROOT)
        self._copy_static_root_to_temp_dir()
        if settings.USE_INTEGRATION:
            self._copy_integration_root_to_temp_dir()
        self._compile_conf_and_embed_js()
        self._compile_media_bundles(restrict_bundles, args)
        self._copy_admin_media_to_cache_dir()

        if not self.keeps_previous:
            self._remove_cache_dirs_before(1)

        self._copy_temp_dir_to_cache_dir()
        self._copy_files_with_public_urls_from_cache_dir_to_static_dir()
        self._make_mirosubs_copies_of_files_with_public_urls()

        if self.test_str_version:
            self.test_string_version()

    def test_string_version(self):
        """
        Make sure all the compiled files have the version name appended
        """
        version_str = _make_version_debug_string()
        for file in NO_UNIQUE_URL:
            filename = file['name']
            # we only need compiled sutff (widgetizerprimer breaks the stable urls = compiled assumption
            if os.path.basename(filename) not in settings.MEDIA_BUNDLES.keys():
                continue
            to_path =  os.path.join(settings.STATIC_ROOT, filename)

            data = open(to_path).read()
            assert(data.endswith(version_str))
