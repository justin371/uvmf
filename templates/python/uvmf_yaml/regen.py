import os
import re
from uvmf_gen import UserError
import pprint
import shutil
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

  CUSTOM_PRAGMA_RE = re.compile(
    r'^\s*(?:/{2,}|#+) pragma uvmf custom (\w+) (begin|end)'
  )
  ACTIVE_DEPENDENCY_RE = re.compile(
    r'^\s*"([^"]+)",\s*(?:#.*)?$'
  )

  def __init__(self,outdir,skip_missing_blocks,new_root,old_root,quiet=False,defer_commit=False):
    self.regen = Regen()
    self.copied_files = []
    self.new_root = os.path.realpath(os.path.abspath(os.path.normpath(new_root)))
    self.old_root = os.path.realpath(os.path.abspath(os.path.normpath(old_root)))
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
    self.defer_commit = defer_commit
    self.transaction_backup_dir = None
    self.transaction_backups = {}
    self.transaction_created = set()
    self.transaction_created_dirs = []
    self.ofs = None
    self.tmp_fname = None

  def assert_path_within(self,root,path,description):
    candidate = os.path.realpath(os.path.abspath(os.path.normpath(path)))
    try:
      inside = os.path.commonpath([root,candidate]) == root
    except ValueError:
      inside = False
    if not inside:
      raise UserError(
        'Refusing {0} outside merge root {1}: {2}'.format(
          description,root,path
        )
      )
    return candidate

  def generated_dependencies(self,fname):
    try:
      with open(fname,'r',encoding='utf-8') as handle:
        lines = handle.readlines()
    except UnicodeDecodeError as error:
      raise UserError(
        'Unable to decode generated file as text while preserving custom blocks: {0}'.format(
          fname
        )
      ) from error

    block_index = next((
      index for index,line in enumerate(lines)
      if self.CUSTOM_PRAGMA_RE.search(line)
      and self.CUSTOM_PRAGMA_RE.search(line).groups() == ('deps_additional','begin')
    ),None)
    if block_index is None:
      return set()
    list_start = next((
      index for index in range(block_index,-1,-1)
      if re.search(r'^\s*deps\s*=.*\[',lines[index])
    ),None)
    if list_start is None:
      return set()

    list_end = None
    depth = 0
    for index in range(list_start,len(lines)):
      syntax = re.sub(r'"(?:\\.|[^"\\])*"','""',lines[index])
      syntax = syntax.split('#',1)[0]
      depth += syntax.count('[')-syntax.count(']')
      if index > list_start and depth <= 0:
        list_end = index
        break
    if list_end is None or block_index > list_end:
      return set()

    dependencies = set()
    active_label = None
    for line in lines[list_start:list_end+1]:
      pragma = self.CUSTOM_PRAGMA_RE.search(line)
      if pragma:
        label,kind = pragma.groups()
        if kind == 'begin':
          active_label = label
        elif active_label == label:
          active_label = None
        continue
      if active_label is None:
        dependency = self.ACTIVE_DEPENDENCY_RE.match(line)
        if dependency:
          dependencies.add(dependency.group(1))
    return dependencies

  def deduplicate_generated_dependencies(self,content):
    deduplicated = []
    for line in content.splitlines(True):
      dependency = self.ACTIVE_DEPENDENCY_RE.match(line)
      if dependency and dependency.group(1) in self.current_generated_dependencies:
        continue
      deduplicated.append(line)
    return ''.join(deduplicated)

  def load_data(self,data):
    try:
      RegenValidator().schema(data)
    except MultipleInvalid as e:
      resp = humanize_error(data,e).split('\n')
      raise UserError("Validation of regeneration YAML failed:\n{0}".format(pprint.pformat(resp,indent=2)))
    self.rd = {}
    for f in data:
      abs_f = os.path.realpath(os.path.abspath(os.path.normpath(f)))
      self.rd[abs_f] = data[f]

  def file_begin(self,fname,ignore_unmatched=False):
    ## For clarity, use variable "new_fname" to differentiate it from old_fname
    new_fname = self.assert_path_within(
      self.new_root,fname,'read of generated merge input'
    )
    ## Figure out path of this new file in the 'old' directory structure (may not exist in 'old')
    self.old_fname = self.assert_path_within(
      self.old_root,
      self.replace_basedir(new_fname,self.new_root,self.old_root),
      'write of merged output',
    )
    self.current_generated_dependencies = self.generated_dependencies(new_fname)
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
      if label_name == 'deps_additional':
        # Preserve every hand dependency except an exact active dependency that
        # is also present in the new generated file. Commented and legacy labels
        # are not assumed to be generator-owned.
        old_content = self.deduplicate_generated_dependencies(old_content)
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
    self.ofs = None
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
            os.unlink(self.tmp_fname)
          self.tmp_fname = None
          raise UserError('Potential loss of hand edits:\n  File: {0}\n  Label: "{1}"\nThe new output does not contain this custom block. Restore the generating YAML component, or use --merge_skip_missing_blocks after reviewing the backup.'.format(self.old_fname,l))
    os.chmod(self.tmp_fname,self.old_mode)
    self.pending_replacements.append((self.tmp_fname,self.old_fname))
    self.tmp_fname = None

  def apply_pending(self):
    staged = list(self.pending_replacements)
    copied_destinations = []
    try:
      for source,destination in self.pending_copies:
        destination = self.assert_path_within(
          self.old_root,destination,'copy of generated output'
        )
        self.ensure_destination_directory(os.path.dirname(destination))
        fd,tmp = tempfile.mkstemp(
          prefix='.'+os.path.basename(destination)+'.',
          suffix='.uvmf_merge_tmp',dir=os.path.dirname(destination)
        )
        os.close(fd)
        staged.append((tmp,destination))
        copyfile(source,tmp)
        copied_destinations.append(destination)

      destinations = [destination for source,destination in staged]
      if len(destinations) != len(set(destinations)):
        raise UserError('Internal error - duplicate merge destination')
      self.prepare_transaction(destinations)
      for source,destination in staged:
        os.replace(source,destination)
      self.copied_files.extend(copied_destinations)
      if not self.defer_commit:
        self.commit()
    except BaseException:
      for source,destination in staged:
        if os.path.exists(source):
          try:
            os.unlink(source)
          except OSError:
            pass
      self.rollback()
      raise
    finally:
      for source,destination in staged:
        if os.path.exists(source):
          try:
            os.unlink(source)
          except OSError:
            pass
      self.pending_copies = []
      self.pending_replacements = []

  def ensure_destination_directory(self,directory):
    missing = []
    current = directory
    while current and not os.path.exists(current):
      self.assert_path_within(
        self.old_root,current,'creation of merge output directory'
      )
      missing.append(current)
      parent = os.path.dirname(current)
      if parent == current:
        break
      current = parent
    os.makedirs(directory,exist_ok=True)
    self.transaction_created_dirs.extend(reversed(missing))

  def prepare_transaction(self,destinations):
    if not destinations:
      return
    self.transaction_backup_dir = tempfile.mkdtemp(
      prefix='.uvmf_merge_backup_',dir=os.path.dirname(self.old_root)
    )
    for index,destination in enumerate(destinations):
      destination = self.assert_path_within(
        self.old_root,destination,'transactional merge destination'
      )
      if os.path.exists(destination):
        backup_path = os.path.join(self.transaction_backup_dir,str(index))
        shutil.copy2(destination,backup_path)
        self.transaction_backups[destination] = backup_path
      else:
        self.transaction_created.add(destination)

  def rollback(self):
    failures = []
    for destination,backup_path in self.transaction_backups.items():
      try:
        if os.path.exists(backup_path):
          os.makedirs(os.path.dirname(destination),exist_ok=True)
          shutil.copy2(backup_path,destination)
      except OSError as error:
        failures.append('{0}: {1}'.format(destination,error))
    for destination in self.transaction_created:
      try:
        if os.path.lexists(destination):
          os.unlink(destination)
      except OSError as error:
        failures.append('{0}: {1}'.format(destination,error))
    for directory in reversed(self.transaction_created_dirs):
      try:
        os.rmdir(directory)
      except OSError:
        pass
    self.discard_transaction_backup()
    self.transaction_backups = {}
    self.transaction_created = set()
    self.transaction_created_dirs = []
    if failures:
      raise UserError('Unable to roll back merge:\n  '+'\n  '.join(failures))

  def commit(self):
    self.discard_transaction_backup()
    self.transaction_backups = {}
    self.transaction_created = set()
    self.transaction_created_dirs = []

  def discard_transaction_backup(self):
    if self.transaction_backup_dir:
      shutil.rmtree(self.transaction_backup_dir,ignore_errors=True)
      self.transaction_backup_dir = None

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
    if self.ofs is not None:
      self.ofs.close()
      self.ofs = None
    if self.tmp_fname and os.path.exists(self.tmp_fname):
      try:
        os.unlink(self.tmp_fname)
      except OSError:
        pass
    self.tmp_fname = None
    for source,destination in self.pending_replacements:
      if os.path.exists(source):
        try:
          os.unlink(source)
        except OSError:
          pass
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
    if label_name in self.data[fname]:
      raise UserError(
        'Duplicate custom block label in file:\n  File: {0}\n  Label: "{1}"\n  Second begin line: {2}'.format(
          fname,label_name,begin_line
        )
      )
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
    seen_labels = set()
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
              if label_name in seen_labels:
                raise UserError(
                  'Duplicate custom block label in file:\n  File: {0}\n  Label: "{1}"\n  Second begin line: {2}'.format(
                    fname,label_name,begin_line
                  )
                )
              seen_labels.add(label_name)
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
    except UnicodeDecodeError as error:
      raise UserError(
        'Unable to decode file as text while preserving custom blocks: {0}'.format(
          fname
        )
      ) from error
    # If we finish parsing the file and we still think we're in a pragma block, flag it as an error
    if (in_block==True):
      raise UserError("Reached end of file while still in custom block:\n  File: {0}\n  Label: {1}\n  Label start line:{2}".format(fname,label_name,begin_line))
    if (post_open_fn != None):
      post_open_fn(fname)
