# Copyright 2013 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""This module contains the Module class and other classes for resources.

The Module class represents a module in the trace viewer system. A module has
a name, and may require a variety of other resources, such as stylesheets,
template objects, raw javascript, or other modules.

Other resources include HTML templates, raw javascript files, and stylesheets.
"""

import os
import re
import inspect

from tvcm import resource as resource_module
from tvcm import js_utils


class DepsException(Exception):
  """Exceptions related to module dependency resolution."""
  def __init__(self, fmt, *args):
    import style_sheet as style_sheet_module
    context = []
    frame = inspect.currentframe()
    while frame:
      locals = frame.f_locals

      module_name = None
      if 'self' in locals:
        s = locals['self']
        if isinstance(s, Module):
          module_name = s.name
        if isinstance(s, style_sheet_module.StyleSheet):
          module_name = s.name + '.css'
      if not module_name:
        if 'module' in locals:
          module = locals['module']
          if isinstance(s, Module):
            module_name = module.name
        elif 'm' in locals:
          module = locals['m']
          if isinstance(s, Module):
            module_name = module.name

      if module_name:
        if len(context):
          if context[-1] != module_name:
            context.append(module_name)
        else:
          context.append(module_name)

      frame = frame.f_back

    context.reverse()
    self.context = context
    context_str = '\n'.join(['  %s' % x for x in context])
    Exception.__init__(self, 'While loading:\n%s\nGot: %s' % (context_str, (fmt % args)))


class ModuleDependencyMetadata(object):
  def __init__(self):
    self.dependent_module_names = []
    self.dependent_raw_script_relative_paths = []
    self.style_sheet_names = []

  def AppendMetdata(self, other):
    self.dependent_module_names += other.dependent_module_names
    self.dependent_raw_script_relative_paths += \
        other.dependent_raw_script_relative_paths
    self.style_sheet_names += other.style_sheet_names


class Module(object):
  """Represents a javascript module.

  Interesting properties include:
    name: Module name, may include a namespace, e.g. 'tvcm.foo'.
    filename: The filename of the actual module.
    contents: The text contents of the module
    dependent_modules: Other modules that this module depends on.

  In addition to these properties, a Module also contains lists of other
  resources that it depends on.
  """
  def __init__(self, loader, name, resource, load_resource=True):
    assert isinstance(name, basestring), 'Got %s instead' % repr(name)
    self.loader = loader
    self.name = name
    self.resource = resource

    if load_resource:
      f = open(self.filename, 'r')
      self.contents = f.read()
      f.close()
    else:
      self.contents = None

    # Dependency metadata, set up during Parse().
    self.dependency_metadata = None

    # Actual dependencies, set up during load().
    self.dependent_modules = []
    self.dependent_raw_scripts = []
    self.style_sheets = []

    # Caches.
    self._all_dependent_modules_recursive = None

  def __repr__(self):
    return '%s(%s)' % (self.__class__.__name__, self.name)

  @property
  def filename(self):
    return self.resource.absolute_path

  def isComponent(self):
    return "/third_party/components/" in self.filename

  def Parse(self):
    """Parses self.contents and fills in the module's dependency metadata."""
    raise NotImplementedError()

  def GetTVCMDepsModuleType(self):
    """Returns the tvcm.setModuleInfo type for this module"""
    raise NotImplementedError()

  def AppendJSContentsToFile(self,
                             f,
                             use_include_tags_for_scripts,
                             dir_for_include_tag_root):
    """Appends the js for this module to the provided file."""
    for dependent_raw_script in self.dependent_raw_scripts:
      if use_include_tags_for_scripts:
        rel_filename = os.path.relpath(dependent_raw_script.filename,
                                       dir_for_include_tag_root)
        f.write("""<include src="%s">\n""" % rel_filename)
      else:
        f.write(js_utils.EscapeJSIfNeeded(dependent_raw_script.contents))
        f.write('\n')

  def AppendHTMLContentsToFile(self, f, ctl):
    """Appends the html for this module [without links] to the provided file."""
    pass

  def Load(self):
    """Loads the sub-resources that this module depends on from its dependency metadata.

    Raises:
      DepsException: There was a problem finding one of the dependencies.
      Exception: There was a problem parsing a module that this one depends on.
    """
    assert self.name, 'Module name must be set before dep resolution.'
    assert self.filename, 'Module filename must be set before dep resolution.'
    assert self.name in self.loader.loaded_modules, 'Module must be registered in resource loader before loading.'

    metadata = self.dependency_metadata
    for name in metadata.dependent_module_names:
      module = self.loader.LoadModule(module_name=name)
      self.dependent_modules.append(module)

    for relative_raw_script_path in metadata.dependent_raw_script_relative_paths:
      raw_script = self.loader.LoadRawScript(relative_raw_script_path)
      self.dependent_raw_scripts.append(raw_script)

    for name in metadata.style_sheet_names:
      style_sheet = self.loader.LoadStyleSheet(name)
      self.style_sheets.append(style_sheet)

  @property
  def all_dependent_modules_recursive(self):
    if self._all_dependent_modules_recursive:
      return self._all_dependent_modules_recursive

    self._all_dependent_modules_recursive = set(self.dependent_modules)
    for dependent_module in self.dependent_modules:
      self._all_dependent_modules_recursive.update(
          dependent_module.all_dependent_modules_recursive)
    return self._all_dependent_modules_recursive

  def ComputeLoadSequenceRecursive(self, load_sequence, already_loaded_set,
                                   depth=0):
    """Recursively builds up a load sequence list.

    Args:
      load_sequence: A list which will be incrementally built up.
      already_loaded_set: A set of modules that has already been added to the
          load sequence list.
      depth: The depth of recursion. If it too deep, that indicates a loop.
    """
    if depth > 32:
      raise Exception('Include loop detected on %s', self.name)
    for dependent_module in self.dependent_modules:
      if dependent_module.name in already_loaded_set:
        continue
      dependent_module.ComputeLoadSequenceRecursive(
          load_sequence, already_loaded_set, depth+1)
    if self.name not in already_loaded_set:
      already_loaded_set.add(self.name)
      load_sequence.append(self)

  def GetAllDependentFilenamesRecursive(self, include_raw_scripts=True):
    dependent_filenames = []

    visited_modules = set()
    def Get(module):
      module.AppendDirectlyDependentFilenamesTo(
          dependent_filenames, include_raw_scripts)
      visited_modules.add(module)
      for m in module.dependent_modules:
        if m in visited_modules:
          continue
        Get(m)

    Get(self)
    return dependent_filenames

  def AppendDirectlyDependentFilenamesTo(
      self, dependent_filenames, include_raw_scripts=True):
    dependent_filenames.append(self.resource.absolute_path)
    if include_raw_scripts:
      for raw_script in self.dependent_raw_scripts:
        dependent_filenames.append(raw_script.resource.absolute_path)
    for style_sheet in self.style_sheets:
      style_sheet.AppendDirectlyDependentFilenamesTo(dependent_filenames)

class RawScript(object):
  """Represents a raw script resource referenced by a module via the
  tvcm.requireRawScript(xxx) directive."""
  def __init__(self, resource):
    self.resource = resource

  @property
  def filename(self):
    return self.resource.absolute_path

  @property
  def contents(self):
    return self.resource.contents

  def __repr__(self):
    return "RawScript(%s)" % self.filename
