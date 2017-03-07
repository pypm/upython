# Copyright (c) 2017 Niklas Rosenstein
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
"""
Node.py is a loader for Python modules in the Node.js-style. Unlike standard
Python modules, the Node.py `require()` caches modules by their filename and
thus allows modules with the same name be loaded from multiple locations at
the same time.
"""

from __future__ import absolute_import, division, print_function

__author__ = 'Niklas Rosenstein <rosensteinniklas@gmail.com>'
__version__ = '0.0.12'
__license__ = 'MIT'

import argparse
import code
import collections
import contextlib
import itertools
import marshal
import os
import pdb
import sys
import traceback
import types

import localimport
import six

try:
  import importlib._bootstrap_external
except ImportError:
  importlib = None


VERSION = 'Node.py-{0} [Python {1}.{2}.{3}]'.format(__version__, *sys.version_info)

PackageLink = collections.namedtuple('PackageLink', 'src dst')


@contextlib.contextmanager
def jit_debug(debug=True):
  """
  A context-manager that debugs the exception being raised inside the context.
  Can be disabled by setting *debug* to #False. The exception will be re-raised
  either way.
  """

  try:
    yield
  except BaseException as exc:
    if debug:
      pdb.post_mortem(sys.exc_info()[2])
    raise


def _get_name(x):
  if hasattr(x, '__name__'):
    return x.__name__
  return type(x).__name__


def new_module(name):
  """
  Creates a new #types.ModuleType object from the specified *name*. In Python
  2, the constructor accepts only normal strings and not unicode (which is what
  we get from #click though).
  """

  if six.PY2 and isinstance(name, unicode):
    name = name.encode()
  return types.ModuleType(name)


class ResolveError(Exception):
  def __init__(self, request, current_dir, is_main, path):
    self.request = request
    self.current_dir = current_dir
    self.is_main = is_main
    self.path = path


class UnknownModuleTypeError(Exception):
  def __filename__(self, filename):
    self.filename = filename


class BaseModule(object):
  """
  Represents a Python module that exposes members like data, functions and
  classes in its #namespace.
  """

  def __init__(self, context, filename, directory, name):
    self.context = context
    self.filename = filename
    self.directory = directory
    self.name = name
    self.namespace = new_module(name)
    self.executed = False
    self.init_namespace()

  def init_namespace(self):
    self.namespace.__file__ = self.filename
    self.namespace.__name__ = self.name
    self.namespace.require = Require(self)
    self.namespace.module = self
    if self.directory:
      self.namespace.__directory__ = self.directory

  def exec_(self):
    raise NotImplementedError


class InteractiveSessionModule(BaseModule):
  """
  A proxy module used for interactive sessions.
  """

  def __init__(self, context):
    super(InteractiveSessionModule, self).__init__(context, '__interactive__',
        os.getcwd(), 'interactive')


class NodepyModule(BaseModule):
  """
  Represents an actual `.py` file.
  """

  def __init__(self, context, filename):
    dirname, base = os.path.split(filename)
    super(NodepyModule, self).__init__(context, filename,
        dirname, os.path.splitext(base)[0])

  def exec_(self):
    if self.executed:
      raise RuntimeError('already executed')
    self.executed = True
    with self.context.enter_module(self):
      exec(self._load_code(), vars(self.namespace))

  def _load_code(self):
    with open(self.filename, 'r') as fp:
      return compile(fp.read(), self.filename, 'exec')


class NodepyByteModule(NodepyModule):
  """
  Represents a `.cpython-XY.pyc` file where X and Y stand for the major and
  minor versio of the Python version that the bytecode was compiled with.
  """

  pyc_suffix = '.cpython-{}{}.pyc'.format(*sys.version_info)

  def _load_code(self):
    with open(self.filename, 'rb') as fp:
      if six.PY3:
        importlib._bootstrap_external._validate_bytecode_header(fp.read(12))
      else:
        fp.read(8)
      return marshal.load(fp)


def _py_loader(context, filename):
  """
  Loader for `.py` files. Before the file is loaded, it checks if there is
  bytecache file with the same name and a timestamp that is at least as new
  and loads that file instead.
  """

  assert filename.endswith('.py')
  bytecache_file = os.path.splitext(filename)[0] + NodepyByteModule.pyc_suffix
  if os.path.isfile(bytecache_file) and os.path.isfile(filename) \
      and os.path.getmtime(bytecache_file) >= os.path.getmtime(filename):
    return NodepyByteModule(context, bytecache_file)
  return NodepyModule(context, filename)


class Require(object):
  """
  The `require()` function for #NodepyModule#s.
  """

  def __init__(self, module):
    self.module = module

  @property
  def context(self):
    return self.module.context

  @property
  def main(self):
    return self.module.context.main_module

  @main.setter
  def main(self, module):
    if module is not None and not isinstance(module, BaseModule):
      raise TypeError('main must be None or BaseModule')
    self.module.context.main_module = None

  @property
  def current(self):
    return self.context.current_module

  def __call__(self, request, current_dir=None, is_main=False, cache=True):
    current_dir = current_dir or self.module.directory
    filename = self.context.resolve(request, current_dir, is_main=is_main)
    module = self.context.load_module(filename, is_main=is_main, cache=cache)
    return get_exports(module)

  def exec_main(self, request, current_dir=None, argv=None, cache=True):
    main, self.main = self.main, None
    argv, sys.argv = sys.argv, sys.argv if argv is None else argv
    try:
      self(request, current_dir, is_main=True, cache=cache)
    finally:
      sys.argv = argv
      self.main = main


def get_exports(module):
  """
  Returns the `exports` member of a #BaseModule.namespace if the member exists,
  otherwise the #BaseModule.namespace is returned.
  """

  if not isinstance(module, BaseModule):
    raise TypeError('module must be a BaseModule instance')
  return getattr(module.namespace, 'exports', module.namespace)


def upiter_directory(current_dir):
  """
  A helper function to iterate over the directory *current_dir* and all of
  its parent directories, excluding `nodepy_modules/` and package-scope
  directories (starting with `@`).
  """

  current_dir = os.path.abspath(current_dir)
  while True:
    dirname, base = os.path.split(current_dir)
    if not base.startswith('@') and base != 'nodepy_modules':
      yield current_dir
    if dirname == current_dir:
      # Can happen on Windows for drive letters.
      break
    current_dir = dirname
  return

def find_nearest_modules_directory(current_dir):
  """
  Finds the nearest `nodepy_modules/` directory to *current_dir* and returns
  it. If no such directory exists, #None is returned.
  """

  for directory in upiter_directory(current_dir):
    result = os.path.join(directory, 'nodepy_modules')
    if os.path.isdir(result):
      return result
  return None


def get_package_link(current_dir):
  """
  Finds a `.nodepy-link` file in *path* or any of its parent directories,
  stopping at the first encounter of a `nodepy_modules/` directory. Returns
  a #PackageLink tuple or #None if no link was found.
  """

  for directory in upiter_directory(current_dir):
    link_file = os.path.join(directory, '.nodepy-link')
    if os.path.isfile(link_file):
      with open(link_file) as fp:
        dst = fp.read().rstrip('\n')
      return PackageLink(directory, dst)
  return None


def try_file(filename, preserve_symlinks=True):
  """
  Returns *filename* if it exists, otherwise #None.
  """

  if os.path.isfile(filename):
    if not preserve_symlinks and not is_main and os.path.islink(filename):
      return os.path.realpath(filename)
    return filename
  return None


class Context(object):
  """
  The context encapsulates the execution of Python modules. It serves as the
  central unit to control the finding, caching and loading of Python modules.
  """

  def __init__(self, current_dir='.', verbose=False):
    # Container for internal modules that can be bound to the context
    # explicitly with the #register_binding() method.
    self._bindings = {}
    # Loaders for file extensions. The default loader for `.py` files is
    # automatically registered.
    self._extensions = {}
    self._extensions_order = []
    self.register_extension('.py', _py_loader)
    self.register_extension(NodepyByteModule.pyc_suffix, NodepyByteModule)
    # Container for cached modules. The keys are the absolute and normalized
    # filenames of the module so that the same file will not be loaded multiple
    # times.
    self._module_cache = {}
    # A stack of modules that are currently being executed. Every module
    # should add itself on the stack when it is executed with #enter_module().
    self._module_stack = []
    # A list of additional search directories. Defaults to the paths specified
    # in the `NODEPY_PATH` environment variable.
    self.path = list(filter(bool, os.getenv('NODEPY_PATH', '').split(os.pathsep)))
    # The main module. Will be set by #load_module().
    self.main_module = None

    # Localimport context for Python modules installed via Pip through PPYM.
    nearest_modules = find_nearest_modules_directory(current_dir)
    if not nearest_modules:
      nearest_modules = os.path.join(current_dir, 'nodepy_modules')
    pip_bin_base = 'Scripts' if os.name == 'nt' else 'bin'
    pip_lib_base = 'Lib' if os.name == 'nt' else 'lib/python{}.{}'.format(*sys.version_info)
    self.importer = localimport.localimport(parent_dir=nearest_modules,
        path=['.pip/' + pip_lib_base, '.pip/' + pip_lib_base + '/site-packages'])
    self.verbose = verbose

  def __enter__(self):
    self.importer.__enter__()

  def __exit__(self, *args):
    return self.importer.__exit__(*args)

  def debug(self, *msg):
    if self.verbose:
      print('debug:', *msg, file=sys.stderr)

  @property
  def current_module(self):
    return self._module_stack[-1] if self._module_stack else None

  @contextlib.contextmanager
  def enter_module(self, module):
    """
    Adds the specified *module* to the stack of currently executed modules.
    A module can not add itself more than once to the stack at a time. This
    method is a context-manager and must be used as such.
    """

    if not isinstance(module, BaseModule):
      raise TypeError('module must be a BaseModule instance')
    if module in self._module_stack:
      raise RuntimeError('a module can only appear once in the module stack')
    self._module_stack.append(module)
    self.debug('loading module:', module.filename)
    try:
      yield
    finally:
      if self._module_stack.pop() is not module:
        raise RuntimeError('module stack corrupted')

  def binding(self, binding_name):
    """
    Loads one of the context bindings and returns it. Bindings can be added
    to a Context using the #register_binding() method.
    """

    return self._bindings[binding_name]

  def register_binding(self, binding_name, obj):
    """
    Registers a binding to the Context under the specified *binding_name*. The
    object can be arbitrary, but there can only be one binding under the one
    specified name at a atime. If the *binding_name* is already allocated, a
    #ValueError is raised.
    """

    if binding_name in self._bindings:
      raise ValueError('binding {!r} already exists'.format(binding_name))
    self._bindings[binding_name] = obj

  def register_extension(self, ext, loader):
    """
    Registers a loader function for the file extension *ext*. The dot should
    be included in the *ext*. *loader* must be a callable that expects the
    Context as its first and the filename to load as its second argument.

    If a loader for *ext* is already registered, a #ValueError is raised.
    """

    if ext in self._extensions:
      raise ValueError('extension {!r} already registered'.format(ext))
    if not callable(loader):
      raise TypeError('loader must be a callable')
    self._extensions[ext] = loader
    self._extensions_order.append(ext)

  def resolve(self, request, current_dir=None, is_main=False, path=None):
    """
    Resolves the *request* to a filename of a module that can be loaded by one
    of the extension loaders. For relative requests (ones starting with `./` or
    `../`), the *current_dir* will be used to generate an absolute request.
    Absolute requests will then be resolved by using #try_file() and the
    extensions that have been registered with #register_extension().

    Dependency requests are those that are neither relative nor absolute and
    are of the format `[@<scope>]<name>[/<module>]`. Such requests are looked
    up in the nearest `nodepy_modules/` directory of the *current_dir* and
    then alternatively in the specified list of directories specified with
    *path*. If *path* is #None, it defaults to the #Context.path.

    If *is_main* is specified, dependency requests are also looked up like
    relative requests before the normal lookup procedure kicks in.

    Raises a #ResolveError if the *request* could not be resolved into a
    filename.
    """

    if request.startswith('./') or request.startswith('../'):
      try:
        return self.resolve(os.path.abspath(os.path.join(current_dir, request)))
      except ResolveError as exc:
        raise ResolveError(request, current_dir, is_main, path)
    elif os.path.isabs(request):
      link = get_package_link(request)
      if link:
        self.debug('follow .nodepy-link \'{}\''.format(link.src))
        self.debug('  maps to \'{}\''.format(link.dst))
        request = os.path.join(link.dst, os.path.relpath(request, link.src))
      filename = try_file(request)
      if filename:
        return filename
      for ext in self._extensions_order:
        filename = try_file(request + ext)
        if filename:
          return filename
      if os.path.isdir(request):
        request = os.path.join(request, 'index')
        return self.resolve(request, current_dir, is_main, path)
      raise ResolveError(request, current_dir, is_main, path)

    if current_dir is None and is_main:
      current_dir = '.'

    path = list(self.path if path is None else path)
    nodepy_modules = find_nearest_modules_directory(current_dir)
    if nodepy_modules:
      path.insert(0, nodepy_modules)
    if is_main:
      path.insert(0, current_dir)

    for directory in path:
      new_request = os.path.join(os.path.abspath(directory), request)
      try:
        return self.resolve(new_request, None)
      except ResolveError:
        pass

    raise ResolveError(request, current_dir, is_main, path)

  def load_module(self, filename, is_main=False, exec_=True, cache=True):
    """
    Loads a module by *filename*. The filename will be converted to an
    absolute path and normalized. If the module is already loaded, the
    cached module will be returned.

    Note that the returned #BaseModule will be ensured to be executed
    unless *exec_* is set to False.
    """

    if is_main and self.main_module:
      raise RuntimeError('context already has a main module')

    filename = os.path.normpath(os.path.abspath(filename))
    if cache and filename in self._module_cache:
      return self._module_cache[filename]
    for ext, loader in six.iteritems(self._extensions):
      if filename.endswith(ext):
        break
    else:
      raise UnknownModuleTypeError(filename)
    module = loader(self, filename)
    if not isinstance(module, BaseModule):
      raise TypeError('loader {!r} did not return a BaseModule instance, '
          'but instead a {!r} object'.format(_get_name(loader), _get_name(module)))
    if cache:
      self._module_cache[filename] = module
    if is_main:
      self.main_module = module
    if exec_ and not module.executed:
      module.exec_()
    return module


def main(argv=None):
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('arguments', nargs='...')
  parser.add_argument('-d', '--debug', action='store_true',
      help='Enter the interactive debugger when an exception would cause '
        'the application to exit.')
  parser.add_argument('-v', '--verbose', action='store_true',
      help='Be verbose about what\'s happening in the Node.py context.')
  parser.add_argument('-c', '--exec', dest='exec_', metavar='EXPR',
      help='Evaluate a Python expression.')
  parser.add_argument('--current-dir', default='.', metavar='DIR',
      help='Change where the initial request will be resolved in.')
  parser.add_argument('--version', action='store_true',
      help='Print the Node.py version and exit.')
  parser.add_argument('--keep-arg0', action='store_true',
      help='Do not overwrite sys.argv[0] when executing a file.')
  args = parser.parse_args(sys.argv[1:] if argv is None else argv)

  if args.version:
    print(VERSION)
    sys.exit(0)

  arguments = args.arguments[:]
  context = Context(args.current_dir, args.verbose)
  with context, jit_debug(args.debug):
    if args.exec_ or not arguments:
      sys.argv = [sys.argv[0]] + arguments
      module = InteractiveSessionModule(context)
      if args.exec_:
        exec(args.exec_, vars(module.namespace))
      else:
        code.interact(VERSION, local=vars(module.namespace))
    else:
      request = arguments.pop(0)
      filename = context.resolve(request, args.current_dir, is_main=True)
      sys.argv = [sys.argv[0] if args.keep_arg0 else filename] + arguments
      module = context.load_module(filename, is_main=True)

  sys.exit(0)


if ('require' in globals() and require.main == module) or __name__ == '__main__':
  sys.exit(main())
