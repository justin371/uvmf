import os
import re
from uvmf_gen import UserError
import pprint
import stat
import tempfile
from uvmf_yaml import RegenValidator

from voluptuous import MultipleInvalid
from voluptuous.humanize import humanize_error

from shutil import copyfile

class Base:

  # Replace the base directory structure with something new, maintaining the top-most path
  def replace_basedir(self, p, old_basedir, new_basedir):
    return os.path.normpath(os.path.join(new_basedir,os.path.relpath(p,old_basedir)))

class Merge(Base):

  TESTBENCH_BUILD_RE = re.compile(
    r'(?:^|[\\/])project_benches[\\/][^\\/]+[\\/]tb[\\/]testbench[\\/]BUILD$'
  )
  TESTS_BUILD_RE = re.compile(
    r'(?:^|[\\/])project_benches[\\/][^\\/]+[\\/]tb[\\/]tests[\\/]BUILD$'
  )
  ENVIRONMENT_BUILD_RE = re.compile(
    r'(?:^|[\\/])verification_ip[\\/]environment_packages[\\/][^\\/]+_env_pkg[\\/]BUILD$'
  )
  TEMPLATE_OWNED_TESTBENCH_DEP_RE = re.compile(
    r'^\s*#?\s*"(?:'
    r'@uvmf//uvmf_base_pkg:pkg|'
    r'//hw/dv/project_benches/[^"/]+/tb/parameters:pkg|'
    r'//hw/dv/project_benches/[^"/]+/tb/tests(?::tests)?|'
    r'//hw/dv/verification_ip/environment_packages/[^"/]+_env_pkg:pkg'
    r')",\s*(?:#.*)?$'
  )
  TEMPLATE_OWNED_TESTS_DEP_RE = re.compile(
    r'^\s*#?\s*"(?:'
    r'@uvmf//uvmf_base_pkg:pkg|'
    r'//hw/dv/project_benches/[^"/]+/tb/parameters:pkg|'
    r'//hw/dv/verification_ip/environment_packages/[^"/]+_env_pkg:pkg'
    r')",\s*(?:#.*)?$'
  )
  TEMPLATE_OWNED_ENVIRONMENT_DEP_RE = re.compile(
    r'^\s*#?\s*"(?:'
    r'@uvmf//uvmf_base_pkg:pkg|'
    r'@dv_common//cmn:pkg|'
    r'@cluelib_pkg//:pkg|'
    r'@svlib_pkg//:pkg|'
    r'//hw/dv/verification_ip/interface_packages/[^"/]+_pkg:pkg|'
    r'//hw/dv/verification_ip/environment_packages/[^"/]+_env_pkg:pkg'
    r')",\s*(?:#.*)?$'
  )

  def __init__(self,outdir,skip_missing_blocks,new_root,old_root,quiet=False):
    self.regen = Regen()
    self.copied_files = []
    self.new_root = new_root
    self.old_root = old_root
    self.copied_old_files = []
    self.found_blocks = {}
    self.new_blocks = {}
    self.outdir = os.path.abspath(os.path.normpath(outdir))
    self.missing_blocks = {}
    self.skip_missing_blocks = skip_missing_blocks
    self.quiet = quiet
    self.new_directories = []
    self.block_copied = False
    self.pending_copies = []
    self.pending_replacements = []

  def load_data(self,data):
    try:
      RegenValidator().schema(data)
    except MultipleInvalid as e:
      resp = humanize_error(data,e).split('\n')
      raise UserError("Validation of regeneration YAML failed:\n{0}".format(pprint.pformat(resp,indent=2)))
    self.rd = {}
    for f in data:
      abs_f = os.path.abspath(os.path.normpath(f))
      self.rd[abs_f] = data[f]

  def file_begin(self,fname,ignore_unmatched=False):
    ## For clarity, use variable "new_fname" to differentiate it from old_fname
    new_fname = fname
    ## Figure out path of this new file in the 'old' directory structure (may not exist in 'old')
    self.old_fname = self.replace_basedir(new_fname,self.new_root,self.old_root)
    ## Check if old file doesn't exist in the new. If it doesn't, we need to copy from new to old
    if not os.path.exists(self.old_fname):
      self.pending_copies.append((new_fname,self.old_fname))
      ## Function returns False if we do not need to process this file any further
      return False
    elif not (self.old_fname in self.rd):
      raise UserError("Internal error - Source file {0} was not properly parsed for named blocks".format(self.old_fname))
    else:
      ## Matched old_fname up with something in the data structure, which means we have a match between old and new.
      ## Write beside the original and replace it only after a complete merge.
      try:
        fd,self.tmp_fname = tempfile.mkstemp(
          prefix='.'+os.path.basename(self.old_fname)+'.',
          suffix='.uvmf_merge_tmp',
          dir=os.path.dirname(self.old_fname),
          text=True,
        )
        self.old_mode = stat.S_IMODE(os.stat(self.old_fname).st_mode)
        self.ofs = os.fdopen(fd,'w')
      except IOError:
        raise UserError("Unable to create temporary merge file for {0}".format(self.old_fname))
      ## Function returns true if we are now processing the file contents
      return True

  def block_begin(self,fname,line,label_name,begin_line):
    ## For clarity, use "new_fname" instead of fname
    new_fname = fname
    ## Write the incoming line regardless of next steps
    self.ofs.write(line)
    if not label_name in self.rd[self.old_fname]:
      # This labeled block was not in the data structure. Note this and move on (it's ok, it just means the label is new)
      if (self.old_fname not in self.new_blocks):
        self.new_blocks[self.old_fname] = []
      self.new_blocks[self.old_fname].append(label_name)
    else:
      # This labeled block was found in the data structure, write the block contents out and make note of the
      # activity
      if (self.old_fname not in self.found_blocks):
        self.found_blocks[self.old_fname] = []
      self.found_blocks[self.old_fname].append({'name':label_name})
      try:
        self.found_blocks[self.old_fname][-1]['begin'] = self.rd[self.old_fname][label_name]['begin_line']
        self.found_blocks[self.old_fname][-1]['end'] = self.rd[self.old_fname][label_name]['end_line']
      except KeyError:
        self.found_blocks[self.old_fname][-1]['begin'] = 0
        self.found_blocks[self.old_fname][-1]['end'] = 0
        pass
      old_content = self.rd[self.old_fname][label_name]['content']
      if label_name == 'deps_additional' and self.TESTBENCH_BUILD_RE.search(self.old_fname):
        old_content = ''.join(
          content_line for content_line in old_content.splitlines(True)
          if not self.TEMPLATE_OWNED_TESTBENCH_DEP_RE.match(content_line)
        )
      if label_name == 'deps_additional' and self.TESTS_BUILD_RE.search(self.old_fname):
        old_content = ''.join(
          content_line for content_line in old_content.splitlines(True)
          if not self.TEMPLATE_OWNED_TESTS_DEP_RE.match(content_line)
        )
      if label_name == 'deps_additional' and self.ENVIRONMENT_BUILD_RE.search(self.old_fname):
        old_content = ''.join(
          content_line for content_line in old_content.splitlines(True)
          if not self.TEMPLATE_OWNED_ENVIRONMENT_DEP_RE.match(content_line)
        )
      # Keep defaults when upgrading from the old empty tb_attributes block.
      if label_name == 'tb_attributes' and not old_content.strip():
        self.block_copied = False
      else:
        self.ofs.write(old_content)
        self.block_copied = True
      # Also update the data structure to note that the label was used (we track this later on)
      self.rd[self.old_fname][label_name]['block_used'] = True

  def block_end(self,fname,line,label_name,end_line):
    # At the end of each block, clear the block_copied flag and write the line out
    self.block_copied = False
    self.ofs.write(line)

  def block_inside(self,fname,label_name,content,lnum):
    # Only write out the contents of the block if we didn't copy it from the data structure earlier.
    # This happens only if the block is new and we didn't find it in the old source
    if self.block_copied == False:
      self.ofs.write(content)

  def block_outside(self,fname,line,lnum):
    # Outside of any block, just copy the line over
    self.ofs.write(line)

  def file_end(self,fname):
    self.ofs.close()
    # At the end of each file, check to see if any blocks from the data structure went unused.
    # Error if option skip_missing_blocks is FALSE, otherwise produce a warning and move on.
    # Do this by looping through all of the labels in the data structue for the given file
    # and look for the 'block_used' entry.  If that is there, all is good. Otherwise, problem.
    for l in self.rd[self.old_fname]:
      if not self.rd[self.old_fname][l].get('block_used',False):
        if self.skip_missing_blocks == True:
          if self.old_fname not in self.missing_blocks:
            self.missing_blocks[self.old_fname] = [ l ]
          else:
            self.missing_blocks[self.old_fname].append(l)
        else:
          if os.path.exists(self.tmp_fname):
            os.remove(self.tmp_fname)
          raise UserError('Potential loss of hand edits:\n  File: {0}\n  Label: "{1}"\nThe new output does not contain this custom block. Restore the generating YAML component, or use --merge_skip_missing_blocks after reviewing the backup.'.format(self.old_fname,l))
    os.chmod(self.tmp_fname,self.old_mode)
    self.pending_replacements.append((self.tmp_fname,self.old_fname))

  def apply_pending(self):
    try:
      for source,destination in self.pending_copies:
        os.makedirs(os.path.dirname(destination),exist_ok=True)
        copyfile(source,destination)
        self.copied_files.append(destination)
      for source,destination in self.pending_replacements:
        os.replace(source,destination)
    finally:
      for source,destination in self.pending_replacements:
        if os.path.exists(source):
          os.remove(source)
      self.pending_copies = []
      self.pending_replacements = []

  def parse_file(self,fname):
    try:
      self.regen.parse_file(fname,pre_open_fn=self.file_begin,block_begin_fn=self.block_begin,block_end_fn=self.block_end,block_inside_fn=self.block_inside,block_outside_fn=self.block_outside,post_open_fn=self.file_end)
      self.apply_pending()
    except:
      self.apply_pending_cleanup()
      raise

  def traverse_dir(self,fname):
    try:
      self.regen.traverse_dir(fname,pre_open_fn=self.file_begin,block_begin_fn=self.block_begin,block_end_fn=self.block_end,block_inside_fn=self.block_inside,block_outside_fn=self.block_outside,post_open_fn=self.file_end)
      self.apply_pending()
    except:
      self.apply_pending_cleanup()
      raise

  def apply_pending_cleanup(self):
    for source,destination in self.pending_replacements:
      if os.path.exists(source):
        os.remove(source)
    self.pending_copies = []
    self.pending_replacements = []

class Parse(Base):

  def __init__(self,root,quiet=False):
    self.data = {}
    self.root = root
    self.block_count = 0
    self.quiet = quiet
    self.regen = Regen()
    self.new_dirs = []
    self.old_dirs = []

  def parse_file(self,fname):
    self.regen.parse_file(fname,pre_open_fn=self.file_begin,block_begin_fn=self.block_begin,block_end_fn=self.block_end,block_inside_fn=self.block_inside)

  def traverse_dir(self,dname):
    self.regen.traverse_dir(dname,pre_open_fn=self.file_begin,block_begin_fn=self.block_begin,block_end_fn=self.block_end,block_inside_fn=self.block_inside,filter_dirs=self.old_dirs)

  def file_begin(self,fname):
    self.data[fname] = {}

  def block_begin(self,fname,line,label_name,begin_line):
    self.data[fname][label_name] = {'content':'', 'begin_line':begin_line}
    self.block_count += 1

  def block_end(self,fname,line,label_name,end_line):
    self.data[fname][label_name]['end_line'] = end_line

  def block_inside(self,fname,label_name,content,lnum):
    self.data[fname][label_name]['content'] += content

  def collect_directories(self,new_root_dir,old_root_dir):
    nrd = os.path.abspath(os.path.normpath(new_root_dir))
    ord = os.path.abspath(os.path.normpath(old_root_dir))
    for root,dirs,files in os.walk(nrd):
      for dir in dirs:
        self.new_dirs.append(root+os.sep+dir)
        self.old_dirs.append(self.replace_basedir(p=root+os.sep+dir,old_basedir=nrd,new_basedir=ord))
    pass

class Regen:

  # This class is designed to traverse a given starting directory and walk through all underlying hierarchy, parsing
  # each file along the way. For each file that is parsed there are hooks defined at different points:
  # - Just prior to opening a given file
  # - When a new label in the file has been found
  # - When the end of a labeled block has been found
  # - For each line of the file whle inside of a labeled block
  # - For each line of the file while outside of a labeled block
  # - When finished parsing the given file

  def traverse_dir(self,dname,pre_open_fn=None,block_begin_fn=None,block_end_fn=None,block_inside_fn=None,post_open_fn=None,block_outside_fn=None,filter_dirs=None):
    dname = os.path.normpath(dname)
    if not os.path.exists(dname):
      raise UserError("Input directory {0} does not exist".format(dname))
    for root,dirs,files in os.walk(dname):
      for file in sorted(files):
        if (not filter_dirs) or (os.path.abspath(root) in filter_dirs):
          self.parse_file(os.path.abspath(root+os.sep+file),pre_open_fn,block_begin_fn,block_end_fn,block_inside_fn,block_outside_fn,post_open_fn)

  def parse_file(self,fname,pre_open_fn=None,block_begin_fn=None,block_end_fn=None,block_inside_fn=None,block_outside_fn=None,post_open_fn=None):
    fname = os.path.normpath(fname)
    in_block = False
    label_name = ""
    if (pre_open_fn != None):
      if pre_open_fn(fname)==False:
        return
    try:
      with open(fname,'r') as fs:
        for lnum,line in enumerate(fs):
          match = re.search(r"^\s*(\/{2,}|#+) pragma uvmf custom (\w+) (begin|end)",line)
          if match:
            # Found a pragma
            label_type = match.group(3)
            # Determine if label type + current state is valid or not
            if (label_type == 'begin') & (in_block == True):
              raise UserError("Detected beginning of nested custom block:\n  File: {0}\n  Line number: {1}\n  Previous label: {2}\n  New label: {3}".format(fname,lnum+1,label_name,match.group(2)))
            elif (label_type == 'end') & (in_block == False):
              raise UserError("Detected end of custom block with no begin:\n  File: {0}\n  Line number: {1}\n  Label: {2}".format(fname,lnum+1,match.group(2)))
            elif label_type == 'begin':
              # Beginning of new label. Log it and move on to next line
              label_name = match.group(2)
              in_block = True
              begin_line = lnum+1
              # Call function for beginning of label
              if (block_begin_fn != None):
                block_begin_fn(fname,line,label_name,begin_line)
            else:
              # End of label
              in_block = False
              # Check that the name of the end label matches the begin
              if match.group(2) != label_name:
                raise UserError("Detected end of custom block with incorrect label:\n  File: {0}\n  Line number: {1}\n  Previous begin label: {2}\n  Incorrect end label: {3}".format(fname,lnum+1,label_name,match.group(2)))
              if (block_end_fn != None):
                if block_end_fn(fname,line,label_name,lnum+1)==False:
                  continue
          elif in_block == True:
            if (block_inside_fn != None):
              block_inside_fn(fname,label_name,line,lnum+1)
          else:
            if (block_outside_fn != None):
              block_outside_fn(fname,line,lnum+1)
    except UnicodeDecodeError:
      # This can occur if reading a binary file with Python3. It's OK, just give up and move
      # to the next file.
      return
    # If we finish parsing the file and we still think we're in a pragma block, flag it as an error
    if (in_block==True):
      raise UserError("Reached end of file while still in custom block:\n  File: {0}\n  Label: {1}\n  Label start line:{2}".format(fname,label_name,begin_line))
    if (post_open_fn != None):
      post_open_fn(fname)
