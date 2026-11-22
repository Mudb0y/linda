# Linda's build on Linux. NVDA's own build, for Windows, is the SCons files beside this, which it
# leaves alone. The program goes to DATADIR/linda/source as NVDA's Python tree, kept whole and private:
# its locale folders hold dictionaries and gestures beside the catalogues, and its top-level modules
# are called config, api and speech. Where the native pieces are is written into that tree, in
# linuxPlatform/installPaths.py.
project('linda',
  meson_version: '>=1.4.0',
  license: 'GPL-2.0-or-later',
  default_options: ['buildtype=release', 'warning_level=1'],
)

fs = import('fs')
pymod = import('python')

src = meson.project_source_root()
prefix = get_option('prefix')
pkglibdir = prefix / get_option('libdir') / 'linda'
appdir = prefix / get_option('datadir') / 'linda' / 'source'
libraries = ['libnvdaVbuf.so', 'libpwsink.so', 'libbre_vbuf.so']
# The key capture helper, a system service of its own that nothing in this build starts. It is
# private to Linda, as the libraries are, and goes beside them.
helpers = ['linda-keyd']

if get_option('program')
  # NVDA pins 3.13, and the code and the add-ons written against it are tested on that alone.
  python = pymod.find_installation('python3.13', required: false)
  if not python.found()
    error('Python 3.13 was not found as python3.13 on PATH; NVDA pins it, so Linda runs on 3.13 only')
  endif
else
  python = pymod.find_installation()
endif

# The native libraries: NVDA's virtual buffer storage, and the engine's PipeWire sink and document
# renderer; and the engine's key capture helper.
prebuilt = get_option('prebuilt_libraries')
if prebuilt != ''
  foreach name : libraries + helpers
    if not fs.is_file(prebuilt / name)
      error('-Dprebuilt_libraries names @0@, which holds no @1@'.format(prebuilt, name))
    endif
    install_data(prebuilt / name, install_dir: pkglibdir, install_mode: 'rwxr-xr-x')
  endforeach
else
  add_languages('cpp', native: false, required: true)
  dependency('libpipewire-0.3')
  cargo = find_program('cargo')

  # Upstream's storage.cpp and utils.cpp unchanged, with the C interface in engine/vbuf. compat/
  # comes first on the include path, so that its log.h stands in for upstream's.
  shared_library('nvdaVbuf',
    'nvdaHelper/vbufBase/storage.cpp',
    'nvdaHelper/vbufBase/utils.cpp',
    'engine/vbuf/vbufLinux.cpp',
    include_directories: include_directories('engine/vbuf/compat', 'nvdaHelper', 'nvdaHelper/vbufBase'),
    implicit_include_directories: false,
    override_options: ['cpp_std=c++20'],
    cpp_args: [
      '-DNDEBUG',
      '-Wno-sign-compare',
      '-Wno-unused-variable',
      '-Wno-unused-but-set-variable',
      '-Wno-delete-non-virtual-dtor',
    ],
    install: true,
    install_dir: pkglibdir,
  )

  vendor = get_option('cargo_vendor_dir')
  vendor_args = vendor == '' ? [] : ['--vendor-dir', vendor]
  cargo_target = meson.current_build_dir() / 'cargo-target'
  custom_target('engine',
    output: ['libpwsink.so', 'libbre_vbuf.so'] + helpers,
    command: [python, src / 'tools' / 'cargo-libraries', vendor_args, cargo, cargo_target, '@OUTDIR@'],
    console: true,
    build_always_stale: true,
    build_by_default: true,
    install: true,
    install_dir: pkglibdir,
    install_mode: 'rwxr-xr-x',
  )
  test('engine core', python,
    args: [src / 'tools' / 'cargo-libraries', '--test', vendor_args, cargo.full_path(), cargo_target],
    suite: 'engine',
    timeout: 1800,
  )
endif

if get_option('keyd_service')
  # Where systemd looks whatever the library directory, as its own packages install.
  systemd_lib = prefix / 'lib'
  configure_file(
    input: 'linux/keyd/linda-keyd.service.in',
    output: 'linda-keyd.service',
    configuration: {'KEYD': pkglibdir / 'linda-keyd'},
    install: true,
    install_dir: systemd_lib / 'systemd' / 'system',
  )
  install_data('linux/keyd/linda-keyd.sysusers',
    rename: 'linda-keyd.conf',
    install_dir: systemd_lib / 'sysusers.d',
  )
  install_data('linux/keyd/linda-keyd.modules-load',
    rename: 'linda-keyd.conf',
    install_dir: systemd_lib / 'modules-load.d',
  )
  install_data('linux/keyd/io.github.mudb0y.linda.keyd.policy',
    install_dir: get_option('datadir') / 'polkit-1' / 'actions',
  )
endif

if get_option('program')
  # What the Linux side imports beyond the standard library, and the GObject typelibs it asks for.
  # The last five are the Markdown extensions md2html builds the user guide with.
  modules = [
    'bleak', 'configobj', 'cryptography', 'detect_secrets', 'fast_diff_match_patch', 'fuzzysearch',
    'gi', 'louis', 'lxml', 'markdown', 'nh3', 'pyphen', 'regex', 'requests', 'rpyc', 'schedule',
    'serial', 'url_normalize', 'wx', 'xkbcommon', 'Xlib',
    'l2m4m', 'markdown_link_attr_modifier', 'mdx_gh_links', 'mdx_truly_sane_lists', 'pymdownx',
  ]
  typelibs = ['Atspi:2.0', 'Gdk:3.0', 'GdkX11:3.0', 'Gtk:3.0']
  # What it imports where it is there, and does without where it is not: pywayland reads the keymap a
  # Wayland compositor gives its clients, and without it a Wayland session's keys are read in the layout
  # the desktop's settings name.
  optional_modules = ['pywayland']
  module_probe = '''
import importlib
import sys

modules, typelibs = sys.argv[1].split(), sys.argv[2].split()
missing = []
for name in modules:
    try:
        importlib.import_module(name)
    except Exception as error:
        missing.append(f"the Python module {name} ({error})")
if "gi" in sys.modules:
    import gi

    for typelib in typelibs:
        namespace, version = typelib.split(":")
        try:
            gi.require_version(namespace, version)
        except ValueError:
            missing.append(f"the GObject typelib {namespace} {version}")
print("; ".join(missing))
'''
  probe = run_command(python, '-c', module_probe, ' '.join(modules), ' '.join(typelibs), check: true)
  if probe.stdout().strip() != ''
    error('@0@ lacks @1@'.format(python.full_path(), probe.stdout().strip()))
  endif
  optional_probe = run_command(python, '-c', module_probe, ' '.join(optional_modules), '', check: true)
  if optional_probe.stdout().strip() != ''
    warning('@0@ lacks @1@, so in a Wayland session keys are read in the layout the desktop\'s settings name, not in the keymap the compositor compiled'.format(python.full_path(), optional_probe.stdout().strip()))
  endif

  dependency('atspi-2')
  icu = dependency('icu-uc')
  louis = dependency('liblouis')
  find_program('msgfmt')

  cldr = get_option('cldr_data')
  if cldr == '' or not fs.is_dir(cldr / 'locale')
    error('-Dcldr_data must name a checkout of nvaccess/nvda-cldr\'s main-out branch, which holds locale/LANG/cldr.dic')
  endif
  tables = get_option('louis_tables')
  if tables == ''
    tables = louis.get_variable(pkgconfig: 'tablesdir')
  endif
  if not fs.is_file(tables / 'en-us-g2.ctb')
    error('@0@ holds no liblouis tables; name them with -Dlouis_tables'.format(tables))
  endif

  espeak_library = get_option('espeak_library')
  espeak_data = get_option('espeak_data')
  if espeak_library == ''
    espeak = dependency('espeak-ng', required: false)
    if not espeak.found()
      error('eSpeak NG was not found by pkg-config as espeak-ng; install its development files, or name a library with -Despeak_library and its data with -Despeak_data')
    endif
    warning('eSpeak NG is the system\'s @0@, which no test run has proven; Linda is tested with the fork NVDA pins'.format(espeak.version()))
    espeak_library = 'libespeak-ng.so.1'
    if espeak_data == ''
      # Debian keeps the data under the multiarch library directory, others under share.
      foreach candidate : [
        espeak.get_variable(pkgconfig: 'libdir') / 'espeak-ng-data',
        espeak.get_variable(pkgconfig: 'prefix') / 'share' / 'espeak-ng-data',
      ]
        if espeak_data == '' and fs.is_dir(candidate / 'voices' / '!v')
          espeak_data = candidate
        endif
      endforeach
    endif
  elif not fs.is_file(espeak_library)
    error('-Despeak_library names @0@, which is not a file'.format(espeak_library))
  endif
  if espeak_data == '' or not fs.is_dir(espeak_data / 'voices' / '!v')
    error('no espeak-ng-data directory was found for eSpeak NG; name it with -Despeak_data')
  endif

  icu_major = icu.version().split('.')[0]
  icu_lib_dir = get_option('icu_lib_dir')
  if icu_lib_dir == ''
    icu_lib_dir = icu.get_variable(pkgconfig: 'libdir')
  endif
  if not fs.is_file(icu_lib_dir / 'libicuuc.so.' + icu_major)
    error('@0@ holds no libicuuc.so.@1@; name the directory that does with -Dicu_lib_dir'.format(icu_lib_dir, icu_major))
  endif

  openevv = get_option('openevv_driver')
  if openevv != '' and not fs.is_file(openevv / 'openevv.py')
    error('-Dopenevv_driver names @0@, which holds no openevv.py'.format(openevv))
  endif

  install_paths = configure_file(
    input: 'linux/installPaths.py.in',
    output: 'installPaths.py',
    configuration: {
      'VBUF_LIB': pkglibdir / 'libnvdaVbuf.so',
      'VBUF_ENGINE': pkglibdir / 'libbre_vbuf.so',
      'PWSINK_LIB': pkglibdir / 'libpwsink.so',
      'ESPEAK_LIB': espeak_library,
      'ESPEAK_DATA_DIR': espeak_data,
      'ICU_LIB_DIR': icu_lib_dir,
      'ICU_VERSION_MAJOR': icu_major,
      'OPENEVV_DRIVER_DIR': openevv,
    },
  )

  # The user guide, the commands quick reference and the change log go beside the program's folder,
  # where NVDA looks for them when it runs from source.
  artefacts = meson.current_build_dir() / 'artefacts'
  docs = meson.current_build_dir() / 'user_docs'
  custom_target('artefacts',
    output: 'artefacts.stamp',
    command: [
      python, src / 'tools' / 'build-artifacts',
      '--out', artefacts,
      '--docs-out', docs,
      '--doc-languages', get_option('doc_languages'),
      '--cldr-dir', cldr,
      '--louis-tables-dir', tables,
      '--stamp', '@OUTPUT@',
    ],
    build_always_stale: true,
    build_by_default: true,
  )
  meson.add_install_script(python, src / 'tools' / 'install-tree', appdir, artefacts,
    meson.current_build_dir() / 'installPaths.py', docs)

  # NVDA's View licence item reads it from beside the program's folder when it runs from source,
  # which on Linux it always does.
  install_data('copying.txt', install_dir: get_option('datadir') / 'linda')
  install_data('copying.txt', install_dir: get_option('datadir') / 'licenses' / 'linda')
  install_data('linux/linda.desktop', install_dir: get_option('datadir') / 'applications')
  install_data('linux/io.github.mudb0y.linda.metainfo.xml', install_dir: get_option('datadir') / 'metainfo')
  install_man('linux/linda.1', 'linux/linda-say.1')

  foreach name, arguments : {'linda': '', 'linda-say': ' say'}
    configure_file(
      input: 'linux/launcher.in',
      output: name,
      configuration: {'PYTHON': python.full_path(), 'APPDIR': appdir, 'ARGUMENTS': arguments},
      install: true,
      install_dir: get_option('bindir'),
      install_mode: 'rwxr-xr-x',
    )
  endforeach
endif

ruff = find_program('ruff', required: false)
if ruff.found()
  # As the dev shell checks the tree, with the scripts in tools/, which have no .py suffix.
  tools = ['tools/build-artifacts', 'tools/cargo-libraries', 'tools/check-upstream-edits', 'tools/install-tree']
  test('ruff check', ruff, args: ['check', '--no-cache', '.', tools], workdir: src, suite: 'lint')
  test('ruff format', ruff, args: ['format', '--check', '--no-cache', '.', tools], workdir: src, suite: 'lint')
endif
desktop_file_validate = find_program('desktop-file-validate', required: false)
if desktop_file_validate.found()
  test('desktop entry', desktop_file_validate, args: [files('linux/linda.desktop')], suite: 'files')
endif
appstreamcli = find_program('appstreamcli', required: false)
if appstreamcli.found()
  test('metainfo', appstreamcli,
    args: ['validate', '--no-net', '--explain', files('linux/io.github.mudb0y.linda.metainfo.xml')],
    suite: 'files',
  )
endif
if fs.exists(src / '.git') and find_program('git', required: false).found()
  test('upstream edits', python, args: [src / 'tools' / 'check-upstream-edits'], workdir: src, suite: 'lint')
endif
