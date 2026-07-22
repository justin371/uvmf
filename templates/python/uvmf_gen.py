#! /usr/bin/env python3

##############################################################################
## Copyright 2015 Mentor Graphics
## All Rights Reserved Worldwide
##
##   Licensed under the Apache License, Version 2.0 (the "License"); you may
##   not use this file except in compliance with the License.  You may obtain
##   a copy of the License at
##
##    http://www.apache.org/license/LICENSE-2.0
##
##   Unless required by applicable law or agreed to in
##   writing, software distributed under the License is
##   distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
##   CONDITIONS OF ANY KIND, either express or implied.  See
##   the License for the specific language governing
##   permissions and limitations under the License.
##
##############################################################################
##
##   Mentor Graphics Inc
##
##############################################################################
##
##   Created by :    Jon Craft & Bob Oden
##   Creation date : April 12 2015
##
##############################################################################
##
##   This module facilitates the creation of UVMF interface packages,
##   environment packages and testbench packages through the use of Jinja2-
##   based template files.
##
##   See templates.README for more information on usage
##
##############################################################################

import os
import time
import re
import inspect
import sys
import stat
import tempfile
import getpass
from optparse import (OptionParser,BadOptionError,AmbiguousOptionError,SUPPRESS_HELP)
from fnmatch import fnmatch
from shutil import copyfile

from uvmf_version import version

__version__ = version

SVH_KEEP_SUFFIXES = (
  '_macros.svh',
  '_typedefs.svh',
  '_typedefs_hdl.svh',
  '_env_typedefs.svh',
  '_imports.svh',
  '_gen.svh',
)

SVH_EXTERNAL_KEEP_NAMES = (
  'uvm_macros.svh',
  'mcd_macros.svh',
  'cmn_tb_top.svh',
  'tb_defines.svh',
  'uvm_tlm_target_socket_decl.svh',
)

#if (sys.version_info[0] < 3):

sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.realpath(__file__)))+"/templates/python");
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.realpath(__file__)))+"/templates/python/python3");

try:
  import jinja2
except ImportError:
  print("ERROR : Jinja2 package not found.  See templates.README for more information")
  print("Python version info:\n{}".format(sys.version))
  sys.exit(1)

## Custom Template Loader - does everything the FileSystemLoader does but
## can specify a list of glob-type filters to pick up only specific template
## file input. Default behavior is to filter nothing and act just like
## the superclass
class FileSystemFilterLoader(jinja2.FileSystemLoader):
  def __init__(self,searchpath,glob='*',encoding='utf-8',followlinks=False):
    super(FileSystemFilterLoader,self).__init__(searchpath,encoding,followlinks)
    if isinstance(glob,str):
      glob = [glob]
    self.glob = glob

  def list_templates(self):
    filtered_found = []
    for fn in super(FileSystemFilterLoader,self).list_templates():
      b = os.path.basename(fn)
      for g in self.glob:
        if fnmatch(b,g):
          filtered_found.append(fn)
    return set(filtered_found)

import xml.parsers.expat

class PassThroughOptionParser(OptionParser):
  """
  An unknown option pass-through implementation of OptionParser.

  When unknown arguments are encountered, bundle with largs adn try again until
  rargs is depleted.

  sys.exit(status) will still be called if a known argument is passed incorrectly
  (e.g. missing arguments or bad argument types, etc.)
  """
  def _process_args(self,largs,rargs,values):
    while rargs:
      try:
        OptionParser._process_args(self,largs,rargs,values)
      except (BadOptionError,AmbiguousOptionError) as e:
        largs.append(e.opt_str)

## Underlying class definitions
class UserError(Exception):
  def __init__(self,value):
    self.value = value
  def __str__(self):
    return str(self.value)

## Base element class for use in other generators
class BaseElementClass(object):
  def __init__(self,name):
    self.name = name
    self.paramDefs = []
    self.hdlPkgParamDefs = []
    self.hvlPkgParamDefs = []
    self.configVariableValues = []
    self.DPIExports = []
    self.DPIImports = []
    self.DPITransDecl = []
    self.DPIFiles = []
    self.DPICompArgs = ""
    self.DPILinkArgs = ""
    self.soName = ""
    self.svLibNames = []
    self.vipLibEnvVariable = 'UVMF_VIP_LIBRARY_HOME'
    self.vipLibEnvVariableNames = []
    self.header = None
    self.interface_location = "interface_packages"
    self.environment_location = "environment_packages"
    self.vip_location = "verification_ip"
    self.bench_location = "project_benches"
    self.relative_vip_from_sim = ".."+os.path.sep+".."+os.path.sep+".."+os.path.sep+"verification_ip"
    self.relative_vip_from_cwd = ".."
    self.relative_bench_from_cwd = ".."
    self.relative_environment_from_cwd = ".."+os.path.sep+".."
    self.relative_interface_from_cwd = ".."+os.path.sep+".."
    self.flat_output = False

  def addParamDef(self,name,type,value=None):
    """Add a parameter to the package"""
    self.paramDefs.append(ParamDef(name,type,value))

  def addHdlPkgParamDef(self,name,type,value):
    """Add a parameter to the package"""
    self.hdlPkgParamDefs.append(ParamDef(name,type,value))

  def addHvlPkgParamDef(self,name,type,value):
    """Add a parameter to the package"""
    self.hvlPkgParamDefs.append(ParamDef(name,type,value))

  def addConfigVariableValue(self,name,value):
    """Add a parameter to the package"""
    self.configVariableValues.append(ConfigVariableValueClass(name,value))

  def addDPIFile(self,name):
    if self.soName == "":
      raise UserError("No DPI shared object name specified for "+self.name+", must call setDPISOName() before calling addDPIFile() or any other DPI routines")
    self.DPIFiles.append(name)

  def setDPISOName(self,value,compArgs="",linkArgs=""):
    self.soName = value
    self.DPICompArgs = compArgs
    self.DPILinkArgs = linkArgs

  def addDPILibName(self,name):
    self.svLibNames.append(name)

  def addDPIExport(self,name):
    if self.soName == "":
      raise UserError("No DPI shared object name specified for "+self.name+", must call setDPISOName() before calling addDPIExport() or any other DPI routines")
    self.DPIExports.append(name)

  def addDPIImport(self,cReturnType,svReturnType, name, cArgs,argumentsList):
    if self.soName == "":
      raise UserError("No DPI shared object name specified for "+self.name+", must call setDPISOName() before calling addDPIImport() or any other DPI routines")
    self.DPIImports.append(DpiImportClass(name, cReturnType,svReturnType, cArgs,argumentsList))

## Base class for all 'interface' type classes (port, config, transaction, etc.)
class BaseElementInterfaceClass(BaseElementClass):
  def __init__(self,name,type,isrand=False,comment="",unpackedDim=""):
    super(BaseElementInterfaceClass,self).__init__(name)
    self.type = type
    self.isrand = isrand
    self.comment = comment
    self.unpackedDim = unpackedDim

## Base class for all 'interface' Constraints type classes
class BaseElementConstraintsClass(BaseElementClass):
  def __init__(self,name,type,comment):
    super(BaseElementConstraintsClass,self).__init__(name)
    self.type = type
    self.comment = comment

## Base class for all 'Environment' type classes
class BaseElementEnvironmentClass(BaseElementClass):
  def __init__(self,name,type,isrand=False):
    super(BaseElementEnvironmentClass,self).__init__(name)
    self.type = type
    self.isrand = isrand

## Class to initialize command-line parser
class UVMFCommandLineParser:
  def __init__(self,version=None,usage=None):
    self.parser = PassThroughOptionParser(version=version,usage=usage)
    self.parser.add_option("-c","--clean",dest="clean",action="store_true",help="Remove only explicitly approved obsolete outputs instead of generating code")
    self.parser.add_option("-q","--quiet",dest="quiet",action="store_true",help="Suppress output while running",default=False)
    self.parser.add_option("-d","--dest_dir",dest="dest_dir",action="store",type="string",help="Override destination directory.  Default is \"$CWD/uvmf_template_output\"",default="./uvmf_template_output")
    self.parser.add_option("-t","--template_dir",dest="template_dir",action="store",type="string",help="Override which template directory to utilize.  Default is relative to location of uvmf_gen.py file")
    self.parser.add_option("-o","--overwrite",dest="overwrite",action="store_true",help="Overwrite existing output files (default is to skip)",default=False)
    self.parser.add_option("--simulator",dest="simulator",action="store",type="choice",choices=("vcs","xcelium"),help="Select the Bazel simulator profile: vcs (default) or xcelium",default="vcs")
    self.parser.add_option("-b","--debug",dest="debug",action="store_true",help=SUPPRESS_HELP,default=False)
    self.parser.add_option("-y","--yaml",dest="yaml",action="store_true",help="Dump YAML file instead of generate code",default=False)

## Base class for the generator types, this is where the create method is defined.
class BaseGeneratorClass(BaseElementClass):
  def __init__(self,name,gen_type):
    super(BaseGeneratorClass,self).__init__(name)
    self.gen_type = gen_type
    self.conditional_array = []

  def skipTemplateOutput(self,fname):
    """Return True for outputs outside the supported repository profile."""
    norm_fname = fname.replace('\\','/')
    bench_prefix = self.bench_location+'/'+self.name+'/'
    if self.gen_type == 'bench' and any(norm_fname.startswith(bench_prefix+directory+'/') for directory in ('sim','rtl','docs')):
      return True
    if norm_fname.endswith(('.F','.vcompile','.vinfo','.compile','.f','/compile.do')):
      return True
    if self.isApprovedObsoleteGeneratedMakefile(norm_fname):
      return True
    return False

  def keepGeneratedSvh(self,path):
    """Keep .svh only for header-like generated files."""
    extension = os.path.splitext(path)[1].lower()
    if extension != '.svh':
      return False
    basename = os.path.basename(path).lower()
    return basename.endswith(SVH_KEEP_SUFFIXES)

  def preferredGeneratedPath(self,path):
    """Default generated source files to .sv unless they are header-like."""
    if self.keepGeneratedSvh(path):
      return path
    root,extension = os.path.splitext(path)
    if extension.lower() == '.svh':
      return root+'.sv'
    return path

  def keepReferencedSvh(self,path):
    """Preserve external and header-style .svh references inside generated content."""
    extension = os.path.splitext(path)[1].lower()
    if extension != '.svh':
      return False
    if self.keepGeneratedSvh(path):
      return True
    basename = os.path.basename(path).lower()
    if basename in SVH_EXTERNAL_KEEP_NAMES:
      return True
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    if stem.startswith('dpi_link_'):
      return True
    return False

  def fileLooksGenerated(self,path):
    """Best-effort check that a stale file is UVMF-generated, not user-authored."""
    try:
      with open(path,'r',encoding='utf-8') as handle:
        prefix = handle.read(512)
    except (OSError,UnicodeDecodeError):
      return False
    if "Created with uvmf_gen version" in prefix:
      return True
    stem = os.path.splitext(os.path.basename(path))[0]
    old_guard = '_'+re.sub(r'[^A-Za-z0-9]+','_',stem).upper()+'__SVH__'
    return ('`ifndef '+old_guard) in prefix

  def isApprovedObsoleteGeneratedSvh(self,path):
    """Remove only stale generated .svh files that now have a .sv replacement."""
    norm_path = path.replace('\\','/')
    if not norm_path.endswith('.svh'):
      return False
    if self.keepGeneratedSvh(norm_path):
      return False
    replacement = os.path.splitext(path)[0]+'.sv'
    if not os.path.isfile(replacement):
      return False
    return self.fileLooksGenerated(path)

  def rewriteGeneratedSvhReferences(self,content):
    """Keep local generated references aligned with the default .sv output policy."""
    if '.svh' not in content:
      return content
    def replace(match):
      path = match.group(0)
      if self.keepReferencedSvh(path):
        return path
      return os.path.splitext(path)[0]+'.sv'
    return re.sub(r'[A-Za-z0-9_./{}-]+\.svh\b',replace,content)

  def isApprovedObsoleteGeneratedMakefile(self,path):
    """Match only UVMF-generated Makefiles that are safe to skip or clean."""
    norm_path = path.replace('\\','/')
    if norm_path.endswith('/sim/Makefile') or norm_path.endswith('/sim/Makefile_mtlb'):
      return True
    if norm_path.endswith('_pkg/Makefile') or norm_path.endswith('_env_pkg/Makefile'):
      return True
    return False

  def normalizeGeneratedSource(self,fname,content):
    """Apply low-risk lint cleanup to generated SV/Verilog source files."""
    if not fname.lower().endswith(('.sv','.svh','.v','.vh','.svp','.vp')):
      return content
    content = self.rewriteGeneratedSvhReferences(content)
    normalized_lines = []
    for line in content.replace('\t','  ').splitlines():
      normalized_lines.append(line.rstrip())
    normalized = '\n'.join(normalized_lines)
    if content.endswith('\n') or content.endswith('\r\n'):
      normalized += '\n'
    normalized = self.stripCallableBannerComments(normalized)
    normalized = self.labelNamedEndKeywords(normalized)
    return self.addIncludeGuard(fname,normalized)

  def stripCallableBannerComments(self,content):
    """Remove generated FUNCTION/TASK banners that only restate the declaration."""
    lines = content.splitlines(True)
    output = []
    index = 0
    while index < len(lines):
      if not re.match(r'^\s*//\s*\*{10,}\s*$',lines[index]):
        output.append(lines[index])
        index += 1
        continue
      end = index+1
      has_callable_label = False
      while end < len(lines):
        stripped = lines[end].strip()
        if stripped and not stripped.startswith('//'):
          break
        has_callable_label = has_callable_label or bool(
          re.match(r'^//\s*(?:FUNCTION|TASK)\s*:',stripped)
        )
        end += 1
      if has_callable_label:
        index = end
        continue
      output.append(lines[index])
      index += 1
    return ''.join(output)

  def addIncludeGuard(self,fname,content):
    extension = os.path.splitext(fname)[1].lower()
    if extension not in ('.sv','.svh'):
      return content
    stem = os.path.splitext(os.path.basename(fname))[0]
    guard = '_'+re.sub(r'[^A-Za-z0-9]+','_',stem).upper()+'__'+extension[1:].upper()+'__'
    if re.search(r'^\s*`ifndef\s+'+re.escape(guard)+r'\s*$',content,re.MULTILINE):
      return content
    return '`ifndef {0}\n`define {0}\n\n{1}\n`endif // {0}\n'.format(guard,content.rstrip())

  def labelNamedEndKeywords(self,content):
    """Add labels to named SystemVerilog construct terminators."""
    patterns = (
      ('endclass',r'^\s*(?:virtual\s+)?class\s+(?:automatic\s+)?([A-Za-z_]\w*)'),
      ('endpackage',r'^\s*package(?:\s+automatic)?\s+([A-Za-z_]\w*)'),
      ('endmodule',r'^\s*module(?:\s+automatic)?\s+([A-Za-z_]\w*)'),
      ('endinterface',r'^\s*interface(?:\s+automatic)?\s+([A-Za-z_]\w*)'),
      ('endprogram',r'^\s*program(?:\s+automatic)?\s+([A-Za-z_]\w*)'),
      ('endgroup',r'^\s*covergroup\s+([A-Za-z_]\w*)'),
      ('endproperty',r'^\s*property\s+([A-Za-z_]\w*)'),
      ('endsequence',r'^\s*sequence\s+([A-Za-z_]\w*)'),
      ('endclocking',r'^\s*clocking\s+([A-Za-z_]\w*)'),
      ('endchecker',r'^\s*checker\s+([A-Za-z_]\w*)'),
    )
    stacks = {keyword:[] for keyword,pattern in patterns}
    stacks['endfunction'] = []
    stacks['endtask'] = []
    lines = []
    in_block_comment = False

    for line in content.splitlines():
      code = line
      if in_block_comment:
        if '*/' not in code:
          lines.append(line)
          continue
        code = code.split('*/',1)[1]
        in_block_comment = False
      code = code.split('//',1)[0]
      while '/*' in code:
        before,after = code.split('/*',1)
        if '*/' in after:
          code = before+after.split('*/',1)[1]
        else:
          code = before
          in_block_comment = True
          break

      for keyword,pattern in patterns:
        match = re.match(pattern,code)
        if match:
          stacks[keyword].append(match.group(1))

      callable_match = re.search(r'\b(function|task)\b',code)
      if callable_match and not re.search(r'\b(extern|pure)\b',code[:callable_match.start()]):
        kind = callable_match.group(1)
        declaration = code[callable_match.end():]
        names = re.findall(r'([A-Za-z_]\w*)\s*\(',declaration)
        if names:
          stacks['end'+kind].append(names[0])
        else:
          declaration = declaration.split(';',1)[0]
          identifiers = re.findall(r'[A-Za-z_]\w*',declaration)
          if identifiers:
            stacks['end'+kind].append(identifiers[-1])

      for keyword,stack in stacks.items():
        if re.search(r'\b'+keyword+r'\b',code):
          if stack:
            name = stack.pop()
            if not re.search(r'\b'+keyword+r'\b\s*:',code):
              line = re.sub(r'\b'+keyword+r'\b',keyword+' : '+name,line,count=1)
          break
      lines.append(line)

    return '\n'.join(lines)+('\n' if content.endswith('\n') else '')

  def pathIsWithinRoot(self,path):
    """Return True only when path resolves inside the configured output root."""
    root = os.path.realpath(self.root)
    candidate = os.path.realpath(path)
    try:
      return os.path.commonpath([root,candidate]) == root
    except ValueError:
      return False

  def cleanupApprovedOutputs(self):
    """Remove only outputs explicitly approved as obsolete for this repository."""
    from uvmf_yaml.obsolete import remove_obsolete_outputs
    bench_roots = None
    if self.gen_type == 'bench':
      bench_root = os.path.join(self.root,self.bench_location,self.name)
      if not self.pathIsWithinRoot(bench_root):
        raise UserError("Refusing cleanup outside destination root: "+bench_root)
      bench_roots = [bench_root]
    remove_obsolete_outputs(self.root,bench_roots,quiet=self.options.quiet == True)

  def writeOutputAtomically(self,full,content,isExecutable=False):
    """Commit a complete generated file without truncating an existing file first."""
    dirpath = os.path.dirname(full)
    fd,tmp = tempfile.mkstemp(prefix='.'+os.path.basename(full)+'.',suffix='.uvmf_tmp',dir=dirpath,text=True)
    try:
      with os.fdopen(fd,'w') as fh:
        fh.write(content)
      if os.path.exists(full):
        os.chmod(tmp,stat.S_IMODE(os.stat(full).st_mode))
      if isExecutable:
        st = os.stat(tmp)
        os.chmod(tmp,st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
      os.replace(tmp,full)
    finally:
      if os.path.exists(tmp):
        os.remove(tmp)

  def runTemplate(self,template_str,desired_conditional="",ExtraTemplateVars={}):
    """Generate a particular template.  Return early without doing anything if desired_conditional
    is non-blank and doesn't match the condidional field in the given template"""
    template = self.templateEnv.get_template(template_str)
    if self.flat_output:
      self.src_dir = ""
    else:
      self.src_dir = "src/"
    templateVars = self.initTemplateVars({ "user" : self.user,
                                           "name" : self.name,
                                           "year" : self.year,
                                           "date" : self.date,
                                           "root_dir" : self.root,
                                           "uvmf_gen_version" : __version__,
                                           "header_content": self.header,
                                           "flat_output": self.flat_output,
                                           "src_dir": self.src_dir,
                                           "vip_location": self.vip_location,
                                           "interface_location": self.interface_location,
                                           "environment_location": self.environment_location,
                                           "bench_location": self.bench_location,
                                           "simulator": self.options.simulator.upper(),
                                           "relative_vip_from_sim": self.relative_vip_from_sim,
                                           "relative_vip_from_cwd": self.relative_vip_from_cwd,
                                           "relative_bench_from_cwd": self.relative_bench_from_cwd,
                                           "relative_environment_from_cwd": self.relative_environment_from_cwd,
                                            "relative_interface_from_cwd": self.relative_interface_from_cwd,
                                          })
    templateVars.update(ExtraTemplateVars)
    templateVars = self.finalizeTemplateVars(template_str,templateVars)
    ## Do any necessary search/replace operations within the fname variable
    try:
      fname = template.module.fname
    except(AttributeError):
      raise UserError("Template '"+template_str+"' has no fname attribute defined, exiting")
    for key in templateVars:
      if type(templateVars[key]) is str:
        # Skip root_dir reference, it isn't supported and causes havoc on Windows
        # if passed into the regexp parser under certain conditions. Certain directory
        # names starting with certain letters can wind up looking like escape sequences
        # that the regexp parser. So far, I'm aware of "\g"
        if (key != 'root_dir'):
          fname = re.sub(r'\{\{'+key+r'\}\}',templateVars[key],fname)
    fname = self.preferredGeneratedPath(fname)
    if self.skipTemplateOutput(fname):
      return
    ## Conditional template consideration. Two possible ways to control things. The 'desired_conditional'
    ## function input is used as an explicit mechanism to produce a particular template file output from
    ## a higher level. The 'conditional_array' class variable is more general and can be used to turn certain groups of
    ## template outputs on or off in a wider sense. Both are compared against a 'conditional' entry in
    ## the given template file.  If the 'conditional' entry is empty then the output will be produced.
    ## Otherwise, the entry is compared against both the 'desired_conditional' input as well as any entries
    ## in the 'conditional_array' list. If 'desired_conditional' is set here then there *must* be a match
    ## against the field in the TMPL file in order to generate output. If 'conditional_array' entries exist
    ## then it is possible for the output to be produced only if the 'conditional' field in the TMPL file is
    ## non-existent or if it matches an entry in the 'conditional_array' list.
    try:
      conditional = template.module.conditional
    except:
      conditional = ""
      pass
    if (desired_conditional != ""):
      ## We've been given a desired_conditional which means that the TMPL *must* have a matching conditional
      ## in order to proceed
      if (conditional != desired_conditional):
        return
    elif (conditional != ""):
      ## TMPL contains a conditional, try to match against a conditional_array entry
      if (conditional not in self.conditional_array):
        return
    ## If we got here it means that we will be producing output
    full = os.path.abspath(os.path.join(self.root,fname))
    if not self.pathIsWithinRoot(full):
      raise UserError("Refusing template output outside destination root: "+full)
    dirpath = os.path.dirname(full)
    try:
      symlink = os.path.abspath(os.path.expandvars(re.sub(r'\{\{name\}\}',self.name,template.module.symlink)))
      ## For a symbolic link, "fname" represents the symbolink link name and
      ##   "symlink" represents the source
      if (os.path.exists(full) & (self.options.overwrite == False)):
        if (self.options.quiet != True):
          print("Skipping symbolic link "+symlink+", already exists")
      else:
        if (self.options.quiet != True):
          print("Creating symbolic link "+full+" pointing to "+symlink)
        if (os.path.exists(dirpath) == False):
          os.makedirs(dirpath)
        fd,tmp = tempfile.mkstemp(prefix='.'+os.path.basename(full)+'.',suffix='.uvmf_tmp',dir=dirpath)
        os.close(fd)
        os.remove(tmp)
        try:
          if (os.name == 'nt'):
            copyfile(symlink,tmp)
          else:
            os.symlink(symlink,tmp)
          os.replace(tmp,full)
        finally:
          if os.path.lexists(tmp):
            os.remove(tmp)
      return
    except AttributeError:
     isSymlink = False
    try:
      isExecutable = template.module.isExecutable
    except AttributeError:
      isExecutable = False
    if (os.path.exists(full) & os.path.isfile(full) & (self.options.overwrite == False)):
      if (self.options.quiet != True):
        print("Skipping "+full+", already exists")
    else:
      if (self.options.quiet != True):
        print("Generating "+full)
      if (os.path.exists(dirpath) == False):
        os.makedirs(dirpath)
      content = self.normalizeGeneratedSource(fname,template.render(templateVars))
      self.writeOutputAtomically(full,content,isExecutable)

  def finalizeTemplateVars(self,template_str,templateVars):
    """Allow a generator type to specialize context for one output template."""
    return templateVars

  def create(self,desired_template='all',parser=None,archive_yaml=True):
    """This exists across all generator classes and will initiate the creation of all files associated
    with the given object type.  Command-line are also availale in any script that calls this
    function, use the --help switch for details."""
    if (parser==None):
      parser = UVMFCommandLineParser(version=__version__)
    (self.options,args) = parser.parser.parse_args()
    if (self.options.debug == False):
      sys.tracebacklimit = 0
    if (self.options.yaml == True):
      import uvmf_yaml
      dumper = uvmf_yaml.Dumper(self)
      uvmf_yaml.YAMLGenerator(dumper.data,self.name+"_"+self.gen_type+".yaml")
      if (self.gen_type == "environment"):
        for d in dumper.util_data:
          uvmf_yaml.YAMLGenerator(d,self.name+"_util_comp_"+list(d['uvmf']['util_components'].keys())[0]+".yaml")
      return
    self.user = getpass.getuser()
    lt = time.localtime()
    self.year = time.strftime("%Y",lt)
    self.date = time.strftime("%Y %b %d",lt)
    # Determine root.  This is where we will be placing output.
    if self.dest_dir_override != None:
      self.options.dest_dir = self.dest_dir_override
    if (self.options.dest_dir != None):
      dest_dir = self.options.dest_dir
      if (os.path.isdir(os.path.abspath(self.options.dest_dir)) == False):
        os.makedirs(os.path.abspath(self.options.dest_dir))
      self.root = os.path.abspath(self.options.dest_dir)
    else:
      # Default location is current working directory plus 'uvmf_template_output'
      dest_dir = "uvmf_template_output"
      self.root = os.path.join(os.getcwd(),"uvmf_template_output")
    if (self.options.clean == True):
      self.cleanupApprovedOutputs()
      return
    # Determine location of template files. Default is a relataive location of this file, ./template_files.
    # If an environment variable UVMF_TEMPLATE_PATH is set, use these paths to find the necessary
    # source.
    if (self.options.template_dir != None):
      template_path = self.options.template_dir
    else:
      template_path = os.path.join(os.path.dirname(inspect.getfile(self.__class__)),"template_files")
    if (os.path.isdir(template_path) == False):
      raise UserError("Specified path \""+template_path+"\" to template directory not valid")
    extra_paths = []
    try:
      extra_paths = os.environ['UVMF_TEMPLATE_PATH'].split(os.pathsep)
    except:
      pass
    paths = []
    for p in extra_paths+[template_path]:
      paths.append(os.path.join(p,self.template_ext_dir))
      paths.append(os.path.join(p,'base_templates'))
    templateLoader = FileSystemFilterLoader(searchpath=paths,glob='*.TMPL')
    self.templateEnv = jinja2.Environment(loader = templateLoader,trim_blocks=True)
    if (desired_template == 'all'):
      templates = self.templateEnv.list_templates()
      if (len(templates) == 0):
        raise UserError("No templates found in "+mypath)
      try:
        templates.remove("base_template.TMPL")
      except:
        pass
    else:
      templates = [desired_template]
    for template_str in templates:
      self.runTemplate(template_str)
    if (archive_yaml == True):
      import uvmf_yaml
      dumper = uvmf_yaml.Dumper(self,is_archive=True)
      # Archive to the anticipated location of the rest of the output for this item
      if (self.gen_type == "interface"):
        ap = dest_dir+"/"+self.vip_location+"/"+self.interface_location+"/"+self.name+"_pkg/yaml"
      elif (self.gen_type == "environment"):
        ap = dest_dir+"/"+self.vip_location+"/"+self.environment_location+"/"+self.name+"_env_pkg/yaml"
      elif (self.gen_type == "bench"):
        ap = dest_dir+"/"+self.bench_location+"/"+self.name+"/yaml"
      else:
        ## Error somewhere.. either a new type of output has been defined or a typo exists somewhere
        raise UserError("Internal error during YAML archive: \""+self.gen_type+"\" is not a recognized output type. Contact Siemens support")
      if (os.path.exists(ap) == False):
        ## YAML directory doesn't exist, create it
        os.makedirs(ap)
      fn = ap+"/"+self.name+"_"+self.gen_type+".yaml"
      if ((self.options.overwrite == True) | (os.path.exists(fn) == False)):
        ## Only generate archive if it isn't already there or if overwrite option is enabled
        uvmf_yaml.YAMLGenerator(dumper.data,fn)
      if (self.gen_type == "environment"):
        for d in dumper.util_data:
          fn = ap+"/"+self.name+"_util_comp_"+list(d['uvmf']['util_components'].keys())[0]+".yaml"
          if ((self.options.overwrite == True) | (os.path.exists(fn) == False)):
            uvmf_yaml.YAMLGenerator(d,fn)

## Extensions from base element class for direct use in generators
class PortClockClass(BaseElementInterfaceClass):
  def __init__(self,name):
    self.name = name

class PortResetClass(BaseElementInterfaceClass):
  def __init__(self,name):
    self.name = name

class PortClass(BaseElementInterfaceClass):
  def __init__(self,name,width,dir,rstValue,type='tri',isrand=False):
    super(PortClass,self).__init__(name,type,isrand)
    self.width = width
    # Width comes in as a string or as a list of strings
    # - If a singleton, then this is a single-dimension (simple) vector
    # - If a comma-separated list, this is a packed MDA port
    if isinstance(width,list):
      self.vector = ''
      for w in width:
        s = self.calc_width_string(w)
        if s == '':
          s = '[1]'
        self.vector = self.vector+s
    else:
      self.vector = self.calc_width_string(width)
    if (dir not in ['input','output','inout']):
      raise UserError("Port direction ("+dir+") must be input, output or inout")
    self.dir = dir
    self.rstValue = rstValue

  def calc_width_string(self,width):
    try:
      w = int(width)
    except ValueError:
      w = width
      pass
    if w == 1:
      return ''
    elif isinstance(w,int):
      return '[{0}:0]'.format(w-1)
    else:
      return '[{0}-1:0]'.format(w)

class InterfaceConfigClass(BaseElementInterfaceClass):
  def __init__(self,name,type,isrand=False,value='',comment="",unpackedDim=""):
    super(InterfaceConfigClass,self).__init__(name,type,isrand,comment,unpackedDim)
    self.value = value

class EnvironmentConfigClass(BaseElementEnvironmentClass):
  def __init__(self,name,type,isrand=False,value='',comment="",unpackedDim=""):
    super(EnvironmentConfigClass,self).__init__(name,type,isrand)
    self.value = value
    self.comment = comment
    self.unpackedDim = unpackedDim

class TypeClass(BaseElementClass):
  def __init__(self,name,type):
    super(TypeClass,self).__init__(name)
    self.type = type

class ParamDef(BaseElementClass):
  def __init__(self,name,type,value):
    super(ParamDef,self).__init__(name)
    self.type = type
    self.value = value

class TransClass(BaseElementInterfaceClass):
  def __init__(self,name,type,isrand=False,iscompare=True,unpackedDim="",comment=""):
    super(TransClass,self).__init__(name,type,isrand,comment,unpackedDim)
    self.iscompare = iscompare

class ConstraintsClass(BaseElementConstraintsClass):
  def __init__(self,name,type,comment):
    super(ConstraintsClass,self).__init__(name,type,comment)

class ParameterValueClass(BaseElementClass):
  def __init__(self,name,value):
    super(ParameterValueClass,self).__init__(name)
    self.value = value

class ConfigVariableValueClass(BaseElementClass):
  def __init__(self,name,value):
    super(ConfigVariableValueClass,self).__init__(name)
    self.value = value

class DpiImportClass(BaseElementClass):
  def __init__(self, name, cType,svType, cArgs,argumentsList):
    super(DpiImportClass,self).__init__(name)
    self.cType = cType
    self.svType = svType
    self.cArgs = cArgs
    self.arguments = argumentsList

class NonUvmfComponentClass(BaseElementClass):
  def __init__(self,name,type,parametersDict):
    super(NonUvmfComponentClass,self).__init__(name)
    self.type = type
    self.parameters = []
    for parameterName in parametersDict:
      self.parameters.append(ParameterValueClass(parameterName,parametersDict[parameterName]))

class QvipMemoryAgentClass(BaseElementClass):
  def __init__(self,name,type,qvipEnv,parametersDict):
    super(QvipMemoryAgentClass,self).__init__(name)
    self.type = type
    self.qvipEnv=qvipEnv
    self.parameters = []
    for parameterName in parametersDict:
      self.parameters.append(ParameterValueClass(parameterName,parametersDict[parameterName]))

class AgentClass(BaseElementClass):
  def __init__(self,name,ifPkg,clk,rst,agentIndex,parametersDict,initResp='INITIATOR'):
    super(AgentClass,self).__init__(name)
    self.ifPkg = ifPkg
    self.clk = clk
    self.rst = rst
    self.agentIndex = agentIndex
    self.parameters = []
    self.initResp = initResp
    for parameterName in parametersDict:
      self.parameters.append(ParameterValueClass(parameterName,parametersDict[parameterName]))

class RegModelClass(BaseElementClass):
  def __init__(self,sequencer, transactionType, adapterType, busMap, useAdapter=True, useExplicitPrediction=True, vipType=False, qvipAgent=False,regModelPkg='',regBlockClass='',regBlockInstance=''):
    super(RegModelClass,self).__init__('')
    self.useAdapter = useAdapter
    self.useExplicitPrediction = useExplicitPrediction
    self.sequencer = sequencer
    self.transactionType = transactionType
    self.adapterType = adapterType
    self.busMap = busMap
    self.vipType = vipType
    self.qvipAgent = qvipAgent
    self.regModelPkg = regModelPkg
    self.regBlockClass = regBlockClass
    self.regBlockInstance = regBlockInstance

class analysisComponentClass(BaseElementClass):
  def __init__(self,keyword,name,aeDict,apDict,qvipAeDict,parametersList):
    super(analysisComponentClass,self).__init__(name)
    self.keyword = keyword
    self.analysisExports = []
    self.analysisPorts = []
    self.qvipAnalysisExports = []
    self.parameters = []
    for aeName in aeDict:
      self.analysisExports.append(AnalysisExportClass(aeName,aeDict[aeName]))
    for apName in apDict:
      self.analysisPorts.append(AnalysisPortClass(apName,apDict[apName]))
    for aeName in qvipAeDict:
      self.qvipAnalysisExports.append(AnalysisExportClass(aeName,qvipAeDict[aeName]))
    for parameter in parametersList:
      try:
        self.parameters.append(ParamDef(parameter['name'],parameter['type'],parameter['value']))
      except KeyError:
        ## Value is optional, so a key error means it wasn't provided
        self.parameters.append(ParamDef(parameter['name'],parameter['type'],None))
        pass

class BfmClass(BaseElementClass):
  def __init__(self,name,ifPkg,clk,rst,activity,parametersDict,sub_env_path,initResp,agentInstName,portsList):
    super(BfmClass,self).__init__(name)
    self.ifPkg = ifPkg
    self.clk = clk
    self.rst = rst
    self.activity = activity
    self.sub_env_path = sub_env_path
    self.initResp = initResp
    self.agent_inst_name = agentInstName
    self.portList= []
    for portName in portsList:
      self.portList.append(portName)
    self.parameters = []
    for parameterName in parametersDict:
      self.parameters.append(ParameterValueClass(parameterName,parametersDict[parameterName]))

class BfmPkgClass(BaseElementClass):
  def __init__(self,name,ifPkg,vipLibEnvVariable):
    super(BfmPkgClass,self).__init__(name)
    self.ifPkg = ifPkg
    self.vipLibEnvVariable = vipLibEnvVariable

class QvipAgentClass(BaseElementClass):
  def __init__(self,name,ifPkg,activity,unique_id,unique_id_with_underscores,sequencer):
    super(QvipAgentClass,self).__init__(name)
    self.ifPkg = ifPkg
    self.activity = activity
    self.unique_id = unique_id
    self.unique_id_with_underscores = unique_id_with_underscores
    self.sequencer = sequencer

class StringInterfaceNamesClass(BaseElementClass):
  def __init__(self,name,value,agent_name,ifPkg,activity,unique_id,unique_id_with_underscores):
    super(StringInterfaceNamesClass,self).__init__(name)
    self.value =value
    self.agent_name = agent_name
    self.ifPkg = ifPkg
    self.activity = activity
    self.unique_id = unique_id
    self.unique_id_with_underscores = unique_id_with_underscores

class SubEnvironmentClass(BaseElementClass):
  def __init__(self,name,envPkg,numAgents,agent_index,parametersDict,regModelPkg,regBlockClass,regBlockInstance,baseAddress=None):
    super(SubEnvironmentClass,self).__init__(name)
    self.envPkg = envPkg
    self.regModelPkg = regModelPkg
    self.regBlockClass = regBlockClass
    self.regBlockInstance = regBlockInstance
    self.baseAddress = baseAddress
    self.numAgents = numAgents
    self.agentMinIndex = agent_index
    self.agentMaxIndex = agent_index+numAgents-1
    self.parameters = []
    for parameterName in parametersDict:
      self.parameters.append(ParameterValueClass(parameterName,parametersDict[parameterName]))

class QvipSubEnvironmentClass(BaseElementClass):
  def __init__(self,name,envPkg,numAgents,agent_index,agentList,envHasICVIP,envHasQVIP):
    super(QvipSubEnvironmentClass,self).__init__(name)
    self.envPkg = envPkg
    self.envHasICVIP = envHasICVIP
    self.envHasQVIP = envHasQVIP
    self.agentList = agentList
    self.numAgents = numAgents
    self.agentMinIndex = agent_index
    self.agentMaxIndex = agent_index+numAgents-1
    self.qvip_if_name = []
    for element in agentList:
      self.qvip_if_name.append(element['name'])

class QvipHdlModuleClass(BaseElementClass):
  def __init__(self,name,envPkg,unique_id,unique_id_with_underscores):
    super(QvipHdlModuleClass,self).__init__(name)
    self.envPkg = envPkg
    self.unique_id = unique_id
    self.unique_id_with_underscores = unique_id_with_underscores
    self.agent_names = []
    self.agent_activities = {}
    self.agent_types = []

class QvipFileListClass(BaseElementClass):
  def __init__(self,name,envPkg,agent_type):
    super(QvipFileListClass,self).__init__(name)
    self.envPkg = envPkg
    self.env_var = str(envPkg).upper()+"_DIR_NAME"
    self.agent_types = []
    self.agent_types.append(str(agent_type))

class QvipConnectionClass(object):
  def __init__(self, output_component, output_port_name, input_component, input_component_export_name, validate):
    self.output_component = output_component
    self.output_port_name = output_port_name
    self.input_component = input_component
    self.input_component_export_name = input_component_export_name
    self.validate = validate

class QvipAPClass(BaseElementClass):
  def __init__(self,name,agent):
    super(QvipAPClass,self).__init__(name)
    self.agent = agent

class VmapClass(BaseElementClass):
  def __init__(self,name,dirName):
    super(VmapClass,self).__init__(name)
    self.dirName = dirName

class AnalysisExportClass(BaseElementClass):
  def __init__(self,name,tType,connection="",QVIPConn=False):
    super(AnalysisExportClass,self).__init__(name)
    self.tType = tType
    self.connection = connection
    self.QVIPConn = QVIPConn

class AnalysisPortClass(BaseElementClass):
  def __init__(self,name,tType,connection=""):
    super(AnalysisPortClass,self).__init__(name)
    self.tType = tType
    self.connection = connection

class analysisComponentInstClass(BaseElementClass):
  def __init__(self,name,type,parametersDict,extDef):
    super(analysisComponentInstClass,self).__init__(name)
    self.type = type
    self.extDef = extDef
    self.parameters = []
    for parameter in parametersDict:
      self.parameters.append(ParameterValueClass(parameter['name'],parameter['value']))

class envScoreboardClass(BaseElementClass):
  def __init__(self,name,sType,tType,parametersDict):
    super(envScoreboardClass,self).__init__(name)
    self.sType = sType
    self.tType = tType
    self.parameters = []
    for parameterName in parametersDict:
      self.parameters.append(ParameterValueClass(parameterName,parametersDict[parameterName]))

class connectionClass(BaseElementClass):
  def __init__(self,name,pName,subscriberName, aeName, validate):
    super(connectionClass,self).__init__(name)
    self.pName = pName
    self.subscriberName = subscriberName
    self.aeName = aeName
    self.validate = validate

class InterfaceClass(BaseGeneratorClass):
  """Use this class to produce files associated with a particular interface or agent package"""

  def __init__(self,name):
    super(InterfaceClass,self).__init__(name,'interface')
    self.template_ext_dir = 'interface_templates'
    self.ports = []
    self.clock = 'defaultClk'
    self.reset = 'defaultRst'
    self.resetAssertionLevel = False
    self.useDpiLink = False
    self.genInBoundStreamingDriver = False
    self.hdlTypedefs = []
    self.external_imports = []
    self.hvlTypedefs = []
    self.transVars = []
    self.transVarsConstraints = []
    self.configVarsConstraints = []
    self.veloceReady = True
    self.configVars = []
    self.responseOperation = '1\'b1'
    self.responseList = []
    self.responseVarNames = []
    self.enableFunctionalCoverage = False
    self.dest_dir_override = None

  def initTemplateVars(self,template):
    template['sigs'] = self.ports
    template['clock'] = self.clock
    template['resetAssertionLevel'] = self.resetAssertionLevel
    template['useDpiLink'] = self.useDpiLink
    template['genInBoundStreamingDriver'] = self.genInBoundStreamingDriver
    template['reset'] = self.reset
    template['inputPorts'] = self.getInputPorts()
    template['outputPorts'] = self.getOutputPorts()
    template['inoutPorts'] = self.getInoutPorts()
    template['veloceReady'] = self.veloceReady
    template['configVars'] = self.configVars
    template['hdlTypedefs'] = self.hdlTypedefs
    template['paramDefs'] = self.paramDefs
    template['configVariableValues'] = self.configVariableValues
    template['hdlPkgParamDefs'] = self.hdlPkgParamDefs
    template['hvlPkgParamDefs'] = self.hvlPkgParamDefs
    template['external_imports'] = self.external_imports
    template['hvlTypedefs'] = self.hvlTypedefs
    template['transVars'] = self.transVars
    template['transVarsConstraints'] = self.transVarsConstraints
    template['configVarsConstraints'] = self.configVarsConstraints
    template['responseOperation'] = self.responseOperation
    template['responseList'] = self.responseList
    template['responseVarNames'] = self.responseVarNames
    template['DPITransDecl'] = self.DPITransDecl
    template['DPIExports'] = self.DPIExports
    template['DPIImports'] = self.DPIImports
    template['DPIFiles'] = self.DPIFiles
    template['DPICompArgs'] = self.DPICompArgs
    template['DPILinkArgs'] = self.DPILinkArgs
    template['soName'] = self.soName
    template['svLibNames'] = self.svLibNames
    template['vipLibEnvVariable'] = self.vipLibEnvVariable
    template['enableFunctionalCoverage'] = self.enableFunctionalCoverage
    return template

  def addPort(self,name,width,dir,rstValue='\'b0', type='tri'):
    """Add an interface port definition"""
    self.ports.append(PortClass(name,width,dir,rstValue,type))

  def addHdlTypedef(self,name,type):
    """Add a typedef to the interface class's hdl typedefs file"""
    self.hdlTypedefs.append(TypeClass(name,type))

  def addHvlTypedef(self,name,type):
    """Add a typedef to the interface class's hvl typedefs file"""
    self.hvlTypedefs.append(TypeClass(name,type))

  def addImport(self,name):
    """Add an import to the interface package declaration  """
    if (name not in self.external_imports):
      self.external_imports.append(name)

  def addTransVar(self,name,type,isrand=False,iscompare=True,unpackedDim="",comment=""):
    """Add a variable to the interface class's sequence item definition"""
    self.transVars.append(TransClass(name,type,isrand,iscompare,unpackedDim,comment))

  def addTransVarConstraint(self,name,type,comment=""):
    """Add a constraint to the interface class's Constraint item definition"""
    self.transVarsConstraints.append(ConstraintsClass(name,type,comment))

  def addConfigVar(self,name,type,isrand=False,value='',comment="",unpackedDim=""):
    """Add a configuration variable to the interface class's configuration object definition"""
    self.configVars.append(InterfaceConfigClass(name,type,isrand,value,comment,unpackedDim))

  def addConfigVarConstraint(self,name,type,comment=""):
    """Add a constraint to the config class's Constraint item definition"""
    self.configVarsConstraints.append(ConstraintsClass(name,type,comment))

  def specifyResponseOperation(self,val):
    """Specify a logical term that indicates the transaction requires a response"""
    self.responseOperation = val

  def specifyResponseData(self,entriesList):
    for entry in entriesList:
      found = 0
      for trans in self.transVars:
        if trans.name == entry:
          self.responseList.append({'name':trans.name,'type':trans.type,'unpacked_dimensions':trans.unpackedDim});
          self.responseVarNames.append(trans.name);
          found = 1
          break
      if (found==0):
        raise UserError("No transaction variable named "+entry+" found")

  def getPorts(self,type):
    p = []
    for port in self.ports:
      if port.dir == type:
        p.append(port)
    return p

  def getOutputPorts(self):
    return self.getPorts('output')

  def getInputPorts(self):
    return self.getPorts('input')

  def getInoutPorts(self):
    return self.getPorts('inout')

  ## Overload of the create function - add some extra loops on the end for conditional components
  def create(self,desired_template='all',parser=None,archive_yaml=True):
    """Interface class specific create function - allows for the production of conditional files"""
    ## We need to generate a list of DPI arguments that are *not* part of the
    ## existing transVars list - this way we can reliably produce a comprehensive
    ## but non-repeating set of variable declarations
    tnames = []
    for t in self.transVars:
      tnames.append(t.name)
    for d in self.DPIImports:
      for a in d.arguments:
        if a['name'] not in tnames:
          try:
            ud = a['unpacked_dimension']
          except KeyError:
            ud = ""
            pass
          self.DPITransDecl.append(TransClass(a['name'],a['type'],False,False,ud))
    super(InterfaceClass,self).create(desired_template,parser,archive_yaml=archive_yaml)
    if self.options.yaml:
      return
    # Generation of DPI link files
    if ( self.useDpiLink ):
      self.runTemplate("interface_driver_proxy.TMPL",'dpi_link',{ "name":self.name,
                                                                  "paramDefs":self.paramDefs,
                                                                  "transVars":self.transVars,
                                                                  "configVars":self.configVars,
                                                                  "responseList":self.responseList,
                                                                  "responseVarNames":self.responseVarNames
                                                                  })
      self.runTemplate("interface_monitor_proxy.TMPL",'dpi_link',{ "name":self.name,
                                                                  "paramDefs":self.paramDefs,
                                                                  "transVars":self.transVars,
                                                                  "configVars":self.configVars
                                                                  })
      self.runTemplate("interface_tc_cpp.TMPL",'dpi_link',{ "name":self.name})
    # Generation of C DPI files
    first = 0
    for DPIFile in self.DPIFiles:
      if (first==0):
        self.runTemplate("c_file.TMPL",'c_file',{ "fileName":DPIFile,
                                                  "name":self.name,
                                                  "DPIImports":self.DPIImports})
        first = 1
      else:
        self.runTemplate("c_file.TMPL",'c_file',{ "fileName":DPIFile,
                                                  "name":self.name,
                                                  "DPIImports":''})
    # Generation of in-bound streaming driver and driver BFM
    if ( self.genInBoundStreamingDriver ):
      self.runTemplate("interface_ibs_driver_bfm.TMPL",'gen_inbound_streaming_driver',
           { "name":self.name,
             "paramDefs":self.paramDefs,
             "transVars":self.transVars,
             "configVars":self.configVars,
             "responseList":self.responseList,
             "responseVarNames":self.responseVarNames,
             "useDpiLink":self.useDpiLink,
             "veloceReady":self.veloceReady,
             "clock":self.clock,
             "reset":self.reset,
             "resetAssertionLevel":self.resetAssertionLevel,
             "inputPorts":self.getInputPorts(),
             "outputPorts":self.getOutputPorts(),
             "inoutPorts":self.getInoutPorts()
           })
      self.runTemplate("interface_ibs_driver.TMPL",'gen_inbound_streaming_driver',
           {"name":self.name,
             "paramDefs":self.paramDefs,
             "transVars":self.transVars,
             "configVars":self.configVars,
             "responseList":self.responseList,
             "responseVarNames":self.responseVarNames
            })

class EnvironmentClass(BaseGeneratorClass):
  """Use this class to generate files associated with an environment package"""
  def __init__(self,name):
    super(EnvironmentClass,self).__init__(name,'environment')
    self.template_ext_dir = 'environment_templates'
    self.typedefs = []
    self.regModels = []
    self.nonUvmfComponents = []
    self.qvipMemoryAgents = []
    self.agents = []
    self.qvip_agents = []
    self.external_imports = []
    self.agentIndex = 0
    self.subEnvironments = []
    self.subEnvironmentRegPackages = []
    self.qvipSubEnvironments = []
    self.qvipConnections = []
    self.qvip_ap_names = []
    self.agent_packages = []
    self.qvip_agent_packages = []
    self.sub_env_packages = []
    self.planned_interface_packages = set()
    self.planned_environment_packages = set()
    self.qvip_sub_env_packages = []
    self.analysisComponents = []
    self.analysisComponentTypes = []
    self.analysisPorts = []
    self.analysisExports = []
    self.impDecls = []
    self.scoreboards = []
    self.connections = []
    self.p2sConns = []
    self.m2sConns = []
    self.c2eConns = []
    self.acTypes = []
    self.configVars = []
    self.configVarsConstraints = []
    self.uvmc_cpp_flags = ""
    self.uvmc_cpp_files = []
    self.uvmc_cpp_link_args = ""
    self.analysis_ports = []
    self.analysis_exports = []
    self.dest_dir_override = None

  def initTemplateVars(self,template):
    template['typedefs'] = self.typedefs
    template['regModels'] = self.regModels;
    template['nonUvmfComponents'] = self.nonUvmfComponents
    template['qvipMemoryAgents'] = self.qvipMemoryAgents
    template['vipMemoryAgents'] = self.qvipMemoryAgents
    template['agents'] = self.agents
    template['qvip_agents'] = self.qvip_agents
    template['vip_agents'] = self.qvip_agents
    template['external_imports'] = self.external_imports
    template['paramDefs'] = self.paramDefs
    template['configVariableValues'] = self.configVariableValues
    template['hvlPkgParamDefs'] = self.hvlPkgParamDefs
    template['subEnvironments'] = self.subEnvironments
    template['hasAddressedSubmodels'] = any(subenv.regModelPkg is not None and subenv.baseAddress is not None for subenv in self.subEnvironments)
    template['subEnvironmentRegPackages'] = self.subEnvironmentRegPackages
    template['qvipSubEnvironments'] = self.qvipSubEnvironments
    template['vipSubEnvironments'] = self.qvipSubEnvironments
    template['qvipConnections'] = self.qvipConnections
    template['vipConnections'] = self.qvipConnections
    template['qvip_ap_names'] = self.qvip_ap_names
    template['vip_ap_names'] = self.qvip_ap_names
    template['agent_pkgs'] = self.agent_packages
    template['qvip_agent_pkgs'] = self.qvip_agent_packages
    template['vip_agent_pkgs'] = self.qvip_agent_packages
    template['env_pkgs'] = self.sub_env_packages
    template['qvip_env_pkgs'] = self.qvip_sub_env_packages
    template['vip_env_pkgs'] = self.qvip_sub_env_packages
    template['analysisComponents'] = self.analysisComponents
    template['acTypes'] = self.acTypes
    template['scoreboards'] = self.scoreboards
    template['connections'] = self.connections
    template['p2sConnections'] = self.p2sConns
    template['m2sConnections'] = self.m2sConns
    template['c2eConnections'] = self.c2eConns
    template['impDecls'] = self.impDecls
    template['configVars'] = self.configVars
    template['configVarsConstraints'] = self.configVarsConstraints
    template['uvmc_cpp_flags'] = self.uvmc_cpp_flags
    template['uvmc_cpp_files'] = self.uvmc_cpp_files
    template['uvmc_cpp_link_args'] = self.uvmc_cpp_link_args
    template['analysis_ports'] = self.analysis_ports
    template['analysis_exports'] = self.analysis_exports
    template['DPIExports'] = self.DPIExports
    template['DPIImports'] = self.DPIImports
    template['DPIFiles'] = self.DPIFiles
    template['DPICompArgs'] = self.DPICompArgs
    template['DPILinkArgs'] = self.DPILinkArgs
    template['soName'] = self.soName
    template['svLibNames'] = self.svLibNames
    return template

  def localBazelPackageAvailable(self,location,package,planned_packages):
    """Return true when a package exists already or will be generated in this run."""
    return (
      package in planned_packages
      or os.path.isfile(os.path.join(self.root,self.vip_location,location,package,'BUILD'))
    )

  def finalizeTemplateVars(self,template_str,templateVars):
    if template_str == 'environment_BUILD.TMPL':
      templateVars['agent_pkgs'] = [
        pkg for pkg in self.agent_packages
        if self.localBazelPackageAvailable(
          self.interface_location,pkg+'_pkg',self.planned_interface_packages
        )
      ]
      templateVars['env_pkgs'] = [
        pkg for pkg in self.sub_env_packages
        if self.localBazelPackageAvailable(
          self.environment_location,pkg+'_env_pkg',self.planned_environment_packages
        )
      ]
    return templateVars

  def addTypedef(self,name,type):
    """Add a typedef to the interface class's typedefs file"""
    self.typedefs.append(TypeClass(name,type))

  def addImport(self,name):
    """Add an import to the environment package declaration  """
    if (name not in self.external_imports):
      self.external_imports.append(name)

  def addNonUvmfComponent(self,name,type, parametersDict={}):
    """Add an agent instantiation to the definition of this environment class"""
    self.nonUvmfComponents.append(NonUvmfComponentClass(name,type,parametersDict))

  def addQvipMemoryAgent(self,name,type,qvipEnv,parametersDict={}):
    """Add an agent instantiation to the definition of this environment class"""
    self.qvipMemoryAgents.append(QvipMemoryAgentClass(name,type,qvipEnv,parametersDict))

  def addVipMemoryAgent(self,name,type,vipEnv,parametersDict={}):
    self.addQvipMemoryAgent(name,type,vipEnv,parametersDict)

  def addAgent(self,name,ifPkg,clk,rst,parametersDict={},initResp='INITIATOR'):
    """Add an agent instantiation to the definition of this environment class"""
    self.agents.append(AgentClass(name,ifPkg,clk,rst,self.agentIndex,parametersDict,initResp))
    self.agentIndex = self.agentIndex + 1
    if (ifPkg not in self.agent_packages):
      self.agent_packages.append(ifPkg)

  def addSubEnv(self,name,envPkg,numAgents,parametersDict={},regModelPkg=None,regBlockClass=None,regBlockInstance='',baseAddress=None):
    if ( regBlockInstance == ''):
      regBlkInst = name+"_rm"
    else:
      regBlkInst = regBlockInstance
    """Add a sub environment instantiation to the definition of this environment class"""
    self.subEnvironments.append(SubEnvironmentClass(name,envPkg,numAgents,self.agentIndex,parametersDict,regModelPkg,regBlockClass,regBlkInst,baseAddress))
    self.agentIndex = self.agentIndex+numAgents
    if (envPkg not in self.sub_env_packages):
      self.sub_env_packages.append(envPkg)
    if (regModelPkg != None and regModelPkg not in self.subEnvironmentRegPackages):
      self.subEnvironmentRegPackages.append(regModelPkg)

  def addQvipSubEnv(self,name,envPkg,agentList,envHasICVIP,envHasQVIP):
    """Add a sub environment instantiation to the definition of this environment class"""
    self.numAgents = agentList.__len__()
    self.qvipSubEnvironments.append(QvipSubEnvironmentClass(name,envPkg,self.numAgents,self.agentIndex,agentList,envHasICVIP,envHasQVIP))
    # line below updates agentIndex after appending info to qvip_if_name array
    self.agentIndex = self.agentIndex+self.numAgents
    if (envPkg not in self.qvip_sub_env_packages):
      self.qvip_sub_env_packages.append(envPkg)
    for element in agentList:
      if element['type'] == 'vip':
        self.qvip_ap_names.append(QvipAPClass(name,element['name']))

  def addVipSubEnv(self,name,envPkg,agentList,envHasICVIP,envHasQVIP):
    self.addQvipSubEnv(name,envPkg,agentList,envHasICVIP,envHasQVIP)

  def addAnalysisPort(self,name,tType,connection=""):
    """Build and connect an analysis port connection of the given name and transaction type"""
    self.analysis_ports.append(AnalysisPortClass(name,tType,connection))

  def addAnalysisExport(self,name,tType,connection=""):
    """Build and connect an analysis export connection of the given name and transaction type"""
    self.analysis_exports.append(AnalysisExportClass(name,tType,connection))

  def addQvipConnection(self, output_component, output_port_name, input_component, input_component_export_name,validate=True):
    """Add a Qvip Connection for the environment package"""
    self.qvipConnections.append(QvipConnectionClass(output_component, output_port_name, input_component, input_component_export_name,validate))

  def addVipConnection(self, output_component, output_port_name, input_component, input_component_export_name,validate=True):
    self.addQvipConnection(output_component, output_port_name, input_component, input_component_export_name,validate)

  def addImpDecl(self,name):
    """Add an impDecl call for this environment package"""
    if (name not in self.impDecls):
      self.impDecls.append(name)

  def addAnalysisComponentType(self,name):
    """Add an analysis component type for use in this environment package"""
    if (name not in self.acTypes):
      self.acTypes.append(name)

  def addConfigVar(self,name,type,isrand=False,value='',comment="",unpackedDim=""):
    """Add a configuration variable to the environment class's configuration object definition"""
    self.configVars.append(EnvironmentConfigClass(name,type,isrand,value,comment,unpackedDim))

  def addConfigVarConstraint(self,name,type,comment=""):
    """Add a constraint to the config class's Constraint item definition"""
    self.configVarsConstraints.append(ConstraintsClass(name,type,comment))

  def defineAnalysisComponent(self,keyword,name,exportDict,portDict,qvipExportDict={},parametersList=[]):
    """Defines a type of analysis component for use later on."""
    ## Register the desired analysis component on the types array
    self.analysisComponentTypes.append(analysisComponentClass(keyword,name,exportDict,portDict,qvipExportDict,parametersList))
    self.addAnalysisComponentType(name)
    ## Add any non-existent imp-decl calls based on contents of the aeDict
    for aeName in exportDict:
      self.addImpDecl(aeName)
    for aeName in qvipExportDict:
      self.addImpDecl(aeName)

  def addRegisterModel(self,sequencer, transactionType, adapterType, busMap, useAdapter=True, useExplicitPrediction=True, vipType="uvmf",qvipAgent=False,regModelPkg=None,regBlockClass=None,regBlockInstance=''):
    """Adds a register model to the environment."""
    if ( regBlockInstance == ''):
      regBlkInst = self.name+"_rm"
    else:
      regBlkInst = regBlockInstance
    ## Register the desired analysis component on the types array
    self.regModels.append(RegModelClass(sequencer,transactionType,adapterType, busMap,useAdapter,useExplicitPrediction,vipType,qvipAgent,regModelPkg,regBlockClass,regBlkInst))

  # addAnalysisComponent(instanceName, analysisComponentType)
  def addAnalysisComponent(self, name, pType, parametersList=[],extDef=False):
    """Add an analysis component instance  to the definition of this environment class"""
    self.analysisComponents.append(analysisComponentInstClass(name,pType,parametersList,extDef))

  # addUvmfScoreboard(instanceName, scoreboardType, transactionType)
  def  addUvmfScoreboard(self, name, sType, tType,parametersDict={}):
    """Add scoreboard instance to the definition of this environment class"""
    self.scoreboards.append(envScoreboardClass(name,sType,tType,parametersDict))

  # addConnection(outputComponentName, outputPortName, inputComponentName, inputPortName)
  def  addConnection(self, name, pName, subscriberName, aeName, validate=True):
    """Add a connection between two components in the definition of this environment class"""
    self.connections.append(connectionClass(name,pName,subscriberName, aeName, validate))

  ## Overload of the create function - add some extra loops on the end for analysis components
  def create(self,desired_template='all',parser=None,archive_yaml=True):
    """Environment class specific create function - allows for the production of multiple analysis component files"""
    ## Prepand the environment config typedef to the front of all util component instantiations, too
    ## Need to do this before call to super.create since these changes will be needed then
    for ac in self.analysisComponents:
      ac.parameters = [ParameterValueClass("CONFIG_T", "CONFIG_T")] +  ac.parameters
    super(EnvironmentClass,self).create(desired_template,parser,archive_yaml=archive_yaml)
    if self.options.yaml:
      return
    for analysisComp in self.analysisComponentTypes:
      ## All analysis components have one parameter at the front that is a typedef for the
      ## parent environment's configuration type. Prepend that to the parameters list now
      analysisComp.parameters = [ParamDef("CONFIG_T","type",None)] + [ParamDef("BASE_T","type","uvm_component")] + analysisComp.parameters
      self.runTemplate(analysisComp.keyword+".TMPL",analysisComp.keyword,{"name":analysisComp.name,
                                                     "env_name":self.name,
                                                     "exports":analysisComp.analysisExports,
                                                     "ports":analysisComp.analysisPorts,
                                                     "qvip_exports":analysisComp.qvipAnalysisExports,
                                                     "vip_exports":analysisComp.qvipAnalysisExports,
                                                     "parameters":analysisComp.parameters,
                                                      })
    for regModel in self.regModels:
      self.runTemplate("reg_model.TMPL",'reg_model',{"env_name":self.name,
                                                     "useAdapter":regModel.useAdapter,
                                                     "useExplicitPrediction":regModel.useExplicitPrediction,
                                                     "sequencer":regModel.sequencer,
                                                     "transactionType":regModel.transactionType,
                                                     "adapterType":regModel.adapterType,
                                                     "busMap":regModel.busMap,
                                                     "regModelPkg":regModel.regModelPkg,
                                                     "regBlockClass":regModel.regBlockClass,
                                                     "regBlockInstance":regModel.regBlockInstance})
    first = 0
    for DPIFile in self.DPIFiles:
      if (first==0):
        self.runTemplate("c_file.TMPL",'c_file',{ "fileName":DPIFile,
                                                  "env_name":self.name,
                                                  "DPIImports":self.DPIImports})
        first = 1
      else:
        self.runTemplate("c_file.TMPL",'c_file',{ "fileName":DPIFile,
                                                  "env_name":self.name,
                                                  "DPIImports":''})

  def addUVMCflags(self,flag):
    """Add compile flags for compilation of SystemC TLM code"""
    self.uvmc_cpp_flags = flag

  def addUVMClinkArgs(self,linkArgs):
    """Add compile flags for compilation of SystemC TLM code"""
    self.uvmc_cpp_link_args = linkArgs

  def addUVMCfile(self,filename):
    """Add SystemC TLM source file"""
    self.uvmc_cpp_files.append(filename)

class BenchClass(BaseGeneratorClass):
  """Use this class to generate files associated with a particular testbench"""

  def __init__(self,name,env_name,parametersDict={}):
    super(BenchClass,self).__init__(name,'bench')
    self.env_name = env_name
    self.template_ext_dir = 'bench_templates'
    self.vinfoDependencies = []
    self.bfms = []
    self.scoreboards = []
    self.bfm_packages = []
    self.bfm_pkg_env_variables = []
    self.qvip_pkg_file_lists = []
    self.vipLibEnvVariableNames = []
    self.qvip_bfms = []
    self.qvip_bfm_packages = []
    self.vip_packages = []
    self.qvip_hdl_modules = []
    self.qvip_hdl_module_list = []
    self.qvip_pkg_env_variables = []
    self.resource_parameter_names = []
    self.veloceReady = True
    self.useCoEmuClkRstGen = False
    self.clockHalfPeriod = '5ns'
    self.clockPhaseOffset = '9ns'
    self.resetAssertionLevel = False
    self.activePassiveDefault = 'ACTIVE'
    self.useDpiLink = False
    self.resetDuration = '200ns'
    self.external_imports = []
    self.vmaps = []
    self.envParamDefs = []
    self.additionalTops = []
    self.topEnvHasRegisterModel = False
    self.regModelPkg = ''
    self.regBlockClass = ''
    self.regBlockInstance = env_name+"_rm"
    self.using_qvip = False
    self.using_vip = False
    self.bench_plusargs = []
    self.used_uvmf_envs = []
    self.used_qvip_envs = []
    self.used_vip_envs = []
    for parameterName in parametersDict:
      self.envParamDefs.append(ParameterValueClass(parameterName,parametersDict[parameterName]))
    self.dest_dir_override = None

  def initTemplateVars(self,template):
    template['env_name'] = self.env_name
    template['vinfoDependencies'] = self.vinfoDependencies
    template['resource_parameter_names'] = self.resource_parameter_names
    template['bfms'] = self.bfms
    template['scoreboards'] = self.scoreboards
    template['bfm_pkgs'] = self.bfm_packages
    template['bfm_pkg_env_variables'] = self.bfm_pkg_env_variables
    template['qvip_pkg_file_lists'] = self.qvip_pkg_file_lists
    template['vip_pkg_file_lists'] = self.qvip_pkg_file_lists
    template['vipLibEnvVariableNames'] = self.vipLibEnvVariableNames
    template['qvip_bfms'] = self.qvip_bfms
    template['vip_bfms'] = self.qvip_bfms
    template['qvip_hdl_modules'] = self.qvip_hdl_modules
    template['vip_hdl_modules'] = self.qvip_hdl_modules
    template['qvip_bfm_pkgs'] = self.qvip_bfm_packages
    template['vip_bfm_pkgs'] = self.qvip_bfm_packages
    template['vip_packages'] = self.vip_packages
    template['qvip_pkg_env_variables'] = self.qvip_pkg_env_variables
    template['vip_pkg_env_variables'] = self.qvip_pkg_env_variables
    template['veloceReady'] = self.veloceReady
    template['useCoEmuClkRstGen'] = self.useCoEmuClkRstGen
    template['clockHalfPeriod'] = self.clockHalfPeriod
    template['clockPhaseOffset'] = self.clockPhaseOffset
    template['resetAssertionLevel'] = self.resetAssertionLevel
    template['useDpiLink'] = self.useDpiLink
    template['resetDuration'] = self.resetDuration
    template['activePassiveDefault'] = self.activePassiveDefault
    template['external_imports'] = self.external_imports
    template['vmaps'] = self.vmaps
    template['paramDefs'] = self.paramDefs
    template['envParamDefs'] = self.envParamDefs
    template['additionalTops'] = self.additionalTops
    template['topEnvHasRegisterModel'] = self.topEnvHasRegisterModel
    template['regModelPkg'] = self.regModelPkg
    template['regBlockClass'] = self.regBlockClass
    template['regBlockInstance'] = self.regBlockInstance
    template['svLibNames'] = self.svLibNames
    template['using_qvip'] = self.using_qvip
    template['using_vip'] = self.using_vip or self.using_qvip
    template['bench_plusargs'] = self.bench_plusargs
    template['usedUvmfEnvs'] = self.used_uvmf_envs
    template['usedQvipEnvs'] = self.used_qvip_envs
    template['usedVipEnvs'] = self.used_vip_envs if len(self.used_vip_envs) else self.used_qvip_envs
    return template

  ## Overload of the create function - insert some conditional considerations
  def create(self,desired_template='all',parser=None,archive_yaml=True):
    """Bench class specific create function - allows for the production of conditional files"""
    if (self.using_qvip or self.using_vip) and ('need_overlay' not in self.conditional_array):
      self.conditional_array.append('need_overlay')
    super(BenchClass,self).create(desired_template,parser,archive_yaml=archive_yaml)

  def addVinfoDependency(self,name):
    """Add a make target to the vinfo target for compiling c source  """
    self.vinfoDependencies.append(name)

  def addImport(self,name):
    """Add an import to the bench package declaration  """
    if (name not in self.external_imports):
      self.external_imports.append(name)

  def addVmap(self,name,dirName):
    """Add a vmap command to bench makefile"""
    self.vmaps.append(VmapClass(name,dirName))

  def addBfm(self,name,ifPkg,clk,rst,activity,parametersDict={},sub_env_path='environment',initResp='INITIATOR',vipLibEnvVariable='UVMF_VIP_LIBRARY_HOME',agentInstName='agent_inst_name',portList=[]):
    """Add a BFM instantiation to the definition of this bench class"""
    package_name=name+"_BFM"
    value_name=name+"_BFM"
    self.resource_parameter_names.append(StringInterfaceNamesClass(package_name,value_name,name,ifPkg,activity,"",""))
    self.bfms.append(BfmClass(name,ifPkg,clk,rst,activity,parametersDict,sub_env_path,initResp,agentInstName,portList))
    if (ifPkg not in self.bfm_packages):
      self.bfm_packages.append(ifPkg)
      self.bfm_pkg_env_variables.append(BfmPkgClass(name,ifPkg,vipLibEnvVariable))
    if (vipLibEnvVariable != 'UVMF_VIP_LIBRARY_HOME'):
      if (vipLibEnvVariable not in self.vipLibEnvVariableNames):
        self.vipLibEnvVariableNames.append(vipLibEnvVariable)

  def addQvipBfm(self,name,ifPkg,activity,unique_id="",sequencer="",vipPkg="",vipType=""):
    """Instantiate the qvip BFMs to the definition of this bench class"""
    package_name=name
    value_name=name
    unique_id_with_underscores=""
    unique_id_no_dots = unique_id.split(".")
    first=1
    for segment in unique_id_no_dots:
      if (first==1):
        unique_id_with_underscores = segment
        first = 0
      else:
        unique_id_with_underscores = unique_id_with_underscores+"_"+segment
    self.resource_parameter_names.append(StringInterfaceNamesClass(package_name,value_name,name,ifPkg,activity,unique_id,unique_id_with_underscores))
    self.qvip_bfms.append(QvipAgentClass(name,ifPkg,activity,unique_id,unique_id_with_underscores,sequencer))
    if (vipPkg not in self.vip_packages):
      self.vip_packages.append(vipPkg)
    if (ifPkg not in self.qvip_bfm_packages):
      self.qvip_bfm_packages.append(ifPkg)
      self.qvip_pkg_env_variables.append(str(ifPkg).upper())    ## PYTHON3
      self.qvip_pkg_file_lists.append(QvipFileListClass(name,ifPkg,vipType))
    else:
      for qvip_icvip_pkg in self.qvip_pkg_file_lists:
        if ( ifPkg == qvip_icvip_pkg.envPkg):
          if (vipType not in qvip_icvip_pkg.agent_types):
            qvip_icvip_pkg.agent_types.append(str(vipType))
    if (unique_id not in self.qvip_hdl_module_list):
      self.qvip_hdl_module_list.append(unique_id)
      self.qvip_hdl_modules.append(QvipHdlModuleClass(name,ifPkg,unique_id,unique_id_with_underscores))
    for hdl_module in self.qvip_hdl_modules:
      if unique_id_with_underscores == hdl_module.unique_id_with_underscores:
        hdl_module.agent_names.append(str(name).upper())       ## PYTHON3
        if (vipType not in hdl_module.agent_types):
          hdl_module.agent_types.append(str(vipType))
        hdl_module.agent_activities.update({(str(name).upper()):activity})
    self.using_qvip = True
    self.using_vip = True
    if 'using_qvip' not in self.conditional_array:
      self.conditional_array.append('using_qvip')
    if 'using_vip' not in self.conditional_array:
      self.conditional_array.append('using_vip')

  def addVipBfm(self,name,ifPkg,activity,unique_id="",sequencer="",vipPkg="",vipType=""):
    self.addQvipBfm(name,ifPkg,activity,unique_id,sequencer,vipPkg,vipType)

  def addTopLevel(self,topName):
    """Add additional top-level module for simulation"""
    self.additionalTops.append(topName)

  def addScoreboard(self, scoreboard):
    self.scoreboards.append(scoreboard)

class PredictorClass(BaseGeneratorClass):
  """Use this class to generate a predictor"""
  def __init__(self,name):
    super(PredictorClass,self).__init__(name,'predictor')
    self.template_ext_dir = 'analysis_templates'
    self.exports = []
    self.ports = []

  def initTemplateVars(self,template):
    template = super(BenchClass,self).initTemplateVars(template)
    template['exports'] = self.exports
    template['ports'] = self.ports
    return template

  def addAnalysisExport(self,name,tType):
    """Add an analysis_export instantiation to the definition of this predictor class"""
    self.exports.append(AnalysisExportClass(name,tType))

  def addAnalysisPort(self,name,tType):
    """Add an analysis_port instantiation to the definition of this predictor class"""
    self.ports.append(AnalysisPortClass(name,tType))

## This script should not be executed stand-alone
if __name__ == '__main__':
  raise UserError("This script is not intended to be called directly - see templates.README for more information")
  search_paths = ['.']
