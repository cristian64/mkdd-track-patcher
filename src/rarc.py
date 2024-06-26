import os 
import time
import logging
import tempfile
import subprocess 

from io import BytesIO
from itertools import chain
from struct import pack, unpack
from .yaz0 import decompress, compress_fast, read_uint32, read_uint16

log = logging.getLogger(__name__)

def write_uint32(f, val):
    f.write(pack(">I", val))

def write_uint16(f, val):
    f.write(pack(">H", val))

def write_uint8(f, val):
    f.write(pack(">B", val))

def write_pad32(f):
    next_aligned_pos = (f.tell() + 0x1F) & ~0x1F

    f.write(b"\x00"*(next_aligned_pos - f.tell()))
    #log.info(hex(f.tell()))
    #log.info(hex(next_aligned_pos))


class CompressionSetting(object):
    def __init__(self, yaz0_fast=False, wszst=False, compression_level="9"):
        self.yaz0_fast = yaz0_fast 
        self.wszst = wszst 
        self.compression_level = compression_level
    
    def run_wszst(self, file):
        if not self.wszst:
            raise RuntimeError("Wszst is not used")
        handle, abspath = tempfile.mkstemp()
        os.close(handle)
        filedata = file.getvalue()
        with open(abspath, "wb") as f:
            #log.info("writing to", abspath)
            f.write(filedata)
            f.close()
        
        outpath = abspath+".yaz0_tmp"
        args = ["wszst", "COMPRESS", abspath, "--dest", outpath, "--compr", self.compression_level]
        try:
            subprocess.run(args, check=True)
        except Exception as err:
            log.error("Encountered error, cleaning up...")
            os.remove(abspath)
            raise 
            
        with open(outpath, "rb") as f:
            compressed_data = f.read()
            
        os.remove(abspath)
        os.remove(outpath)
        
        if len(filedata) >= len(compressed_data):
            return compressed_data 
        else:
            log.warning("Compressed data bigger than original, using uncompressed data")
            return filedata 



FILE = 0x01 
DIRECTORY = 0x02
COMPRESSED = 0x04
DATA_FILE = 0x10 # unsure, opposed to REL file?
REL_FILE = 0x20 # REL = dynamic link libraries
YAZ0 = 0x80 # if not set but COMPRESSED is set, use yay0?

class FileListing(object):
    def __init__(self, is_file, is_dir, is_compressed, is_data, is_rel, is_yaz0):
        self.is_file = is_file 
        self.is_dir = is_dir 
        self.is_compressed = is_compressed
        self.is_data = is_data 
        self.is_rel = is_rel
        self.is_yaz0 = is_yaz0 
    
    @classmethod
    def from_flags(cls, flags):
        if flags & 0x40:
            log.info("Unknown flag 0x40 set")
        if flags & 0x8:
            log.info("Unknown flag 0x8 set")
            
        return cls( flags & FILE != 0,
                    flags & DIRECTORY != 0,
                    flags & COMPRESSED != 0,
                    flags & DATA_FILE != 0,
                    flags & REL_FILE != 0,
                    flags & YAZ0 != 0)
    
    def to_flags(self):
        result = 0
        if self.is_file:
            result |= FILE 
        if self.is_dir:
            result |= DIRECTORY  
        if self.is_compressed:
            result |= COMPRESSED
        if self.is_data:
            result |= DATA_FILE 
        if self.is_rel:
            result |= REL_FILE
        if self.is_yaz0:
            result |= YAZ0 
        
        return result 
    
    def to_string(self):
        result = []
        if self.is_compressed and self.is_yaz0:
            result.append("yaz0_compressed")
        if self.is_rel:
            result.append("rel")
        
        return "|".join(result)
    
    @classmethod
    def from_string(cls, string):
        file = True 
        dir = False 
        data_file = True 
        rel_file = False 
        compressed = False
        yaz0 = False 
        
        result = string.split("|")
        for setting in result:
            if setting == "yaz0_compressed":
                compressed = True 
                yaz0 = True
            elif setting == "rel":
                data_file = False 
                rel_file = True 
        
        return cls(file, dir, compressed, data_file, rel_file, yaz0)
    
    @classmethod
    def default(cls):
        # Default is a uncompressed Data File
        return cls(True, False, False, True, False, False)
    
    def __str__(self):
        return str(self.__dict__)
    
DATA = [0]

# Hashing algorithm taken from Gamma and LordNed's WArchive-Tools, hope it works
def hash_name(name):
    hash = 0
    multiplier = 1
    if len(name) + 1 == 2:
        multiplier = 2
    elif len(name) + 1 >= 3:
        multiplier = 3

    for letter in name:
        hash = (hash*multiplier) & 0xFFFF
        hash = (hash + ord(letter)) & 0xFFFF

    return hash

class StringTable(object):
    def __init__(self):
        self._strings = BytesIO()
        self._stringmap = {}

    def write_string(self, string):
        if string not in self._stringmap:
            offset = self._strings.tell()
            self._strings.write(string.encode("shift-jis"))
            self._strings.write(b"\x00")

            self._stringmap[string] = offset

    def get_string_offset(self, string):
        return self._stringmap[string]

    def size(self):
        return self._strings.tell()#len(self._strings.getvalue())

    def write_to(self, f):
        f.write(self._strings.getvalue())

def stringtable_get_name(f, stringtable_offset, offset):
    current = f.tell()
    f.seek(stringtable_offset+offset)

    stringlen = 0
    while f.read(1) != b"\x00":
        stringlen += 1

    f.seek(stringtable_offset+offset)

    filename = f.read(stringlen)
    try:
        decodedfilename = filename.decode("shift-jis")
    except:
        log.error(f"filename: {filename}")
        log.error("failed")
        raise
    f.seek(current)

    return decodedfilename

def split_path(path): # Splits path at first backslash encountered
    for i, char in enumerate(path):
        if char == "/" or char == "\\":
            if len(path) == i+1:
                return path[:i], None
            else:
                return path[:i], path[i+1:]

    return path, None

class Directory(object):
    def __init__(self, dirname, nodeindex=None):
        self.files = {}
        self.subdirs = {}
        self.name = dirname
        self._nodeindex = nodeindex

        self.parent = None

    @classmethod
    def from_dir(cls, path, follow_symlinks=False):
        dirname = os.path.basename(path)
        log.debug(f"{dirname} {path}")
        dir = cls(dirname)

        #with os.scandir(path) as entries: <- not supported in versions earlier than 3.6 apparently
        for entry in os.scandir(path):
            log.debug(f"{entry.path} {dirname}")
            if entry.is_dir(follow_symlinks=follow_symlinks):
                newdir = Directory.from_dir(entry.path, follow_symlinks=follow_symlinks)
                dir.subdirs[entry.name] = newdir
                newdir.parent = dir

            elif entry.is_file(follow_symlinks=follow_symlinks):
                with open(entry.path, "rb") as f:
                    file = File.from_file(entry.name, f)
                dir.files[entry.name] = file

        return dir



    @classmethod
    def from_node(cls, f, _name, stringtable_offset, globalentryoffset, dataoffset, nodelist, currentnodeindex, parents=None):
        log.debug("=============================")
        log.debug(f"Creating new node with index {currentnodeindex}")
        name, unknown, entrycount, entryoffset = nodelist[currentnodeindex]
        if name is None:
            name = _name 

        newdir = cls(name, currentnodeindex)

        firstentry = globalentryoffset+entryoffset
        log.debug(f"Node {currentnodeindex} {name} {entrycount} {entryoffset}")
        log.debug(f"offset {f.tell()}")
        for i in range(entrycount):
            offset = globalentryoffset + (entryoffset+i)*20
            f.seek(offset)

            fileentry_data = f.read(20)

            fileid, hashcode, flags, padbyte, nameoffset, filedataoffset, datasize, padding = unpack(">HHBBHIII", fileentry_data)
            log.debug(f"offset {hex(firstentry+i*20)} {fileid} {flags} {nameoffset}")

            name = stringtable_get_name(f, stringtable_offset, nameoffset)

            log.debug(f"name {name} {fileid}")

            if name == "." or name == ".." or name == "":
                continue
            log.debug(f"{name} {nameoffset}")

            if (flags & DIRECTORY) != 0 and not (flags & FILE): #fileid == 0xFFFF: # entry is a sub directory
                #fileentrydata = f.read(12)
                #nodeindex, datasize, padding = unpack(">III", fileentrydata)
                nodeindex = filedataoffset

                name = stringtable_get_name(f, stringtable_offset, nameoffset)
                log.debug(f"{name} {hashcode} {hash_name(name)}")


                newparents = [currentnodeindex]
                if parents is not None:
                    newparents.extend(parents)

                if nodeindex in newparents:
                    log.warning(f"Detected recursive directory: {name}")
                    log.warning(f"{newparents} {nodeindex}")
                    log.warning(f"Skipping")
                    continue

                subdir = Directory.from_node(f, name, stringtable_offset, globalentryoffset, dataoffset, nodelist, nodeindex, parents=newparents)
                subdir.parent = newdir

                newdir.subdirs[subdir.name] = subdir


            else: # entry is a file
                if flags & COMPRESSED:
                    log.info("File is compressed")
                if flags & YAZ0:
                    log.info("File is yaz0 compressed")
                f.seek(offset)
                file = File.from_fileentry(f, stringtable_offset, dataoffset, fileid, hashcode, flags, nameoffset, filedataoffset, datasize)
                newdir.files[file.name] = file

        return newdir

    def walk(self, _path=None):
        if _path is None:
            dirpath = self.name
        else:
            dirpath = _path+"/"+self.name

        log.debug(f"Yielding {dirpath}")

        yield (dirpath, self.subdirs.keys(), self.files.keys())

        for dirname, dir in self.subdirs.items():
            log.debug(f"yielding subdir {dirname}")
            yield from dir.walk(dirpath)

    def __getitem__(self, path):
        name, rest = split_path(path)

        if rest is None or rest.strip() == "":
            if name in self.subdirs:
                return self.subdirs[name]
            elif name in self.files:
                return self.files[name]
            else:
                raise FileNotFoundError(f'Unable to find "{path}" in ARC file')
        elif name in self.files:
            raise RuntimeError("File", name, "is a directory in path", path, "which should not happen!")
        else:
            return self.subdirs[name][rest]

    def __setitem__(self, path, entry):
        name, rest = split_path(path)

        if rest is None or rest.strip() == "":
            if isinstance(name, File):
                if name in self.subdirs:
                    raise FileExistsError("Cannot add file, '{}' already exists as a directory".format(path))

                self.files[name] = entry
            elif isinstance(name, Directory):
                if name in self.files:
                    raise FileExistsError("Cannot add directory, '{}' already exists as a file".format(path))

                self.subdirs[name] = entry
            else:
                raise TypeError("Entry should be of type File or Directory but is type {}".format(type(entry)))

        elif name in self.files:
            raise RuntimeError("File", name, "is a directory in path", path, "which should not happen!")
        else:
            return self.subdirs[name][rest]

    def listdir(self, path):
        if path == ".":
            dir = self
        else:
            dir = self[path]

        entries = []
        entries.extend(dir.files.keys())
        entries.extend(dir.subdirs.keys())
        return entries

    def extract_to(self, path):
        current_dirpath = os.path.join(path, self.name)
        os.makedirs(current_dirpath, exist_ok=True)

        for filename, file in self.files.items():
            filepath = os.path.join(current_dirpath, filename)
            with open(filepath, "w+b") as f:
                file.dump(f)

        for dirname, dir in self.subdirs.items():
            dir.extract_to(current_dirpath)
    
    def absolute_path(self):
        name = self.name
        parent = self.parent
        while parent is not None:
            name = parent.name+"/"+name 
            parent = parent.parent 
        
        return name

class File(BytesIO):
    def __init__(self, filename, fileid=None, hashcode=None, flags=None):
        super().__init__()

        self.name = filename
        self._fileid = fileid
        self._hashcode = hashcode
        self._flags = flags
        if flags is not None:
            self.filetype = FileListing.from_flags(flags)
        else:
            self.filetype = FileListing.default()
    def is_yaz0_compressed(self):
        if self._flags & COMPRESSED and not self._flags & YAZ0:
            log.warning(f"Warning, file {self.name} is compressed but not with yaz0!")
        return self.filetype.compressed and self.filetype.yaz0
    
    @classmethod
    def from_file(cls, filename, f):
        file = cls(filename)

        file.write(f.read())
        file.seek(0)

        return file

    @classmethod
    def from_fileentry(cls, f, stringtable_offset, globaldataoffset, fileid, hashcode, flags, nameoffset, filedataoffset, datasize):
        filename = stringtable_get_name(f, stringtable_offset, nameoffset)
        log.debug(f"-----")
        log.debug(f'"File": {len(filename)}')
        log.debug(f'"size": {datasize}')
        log.debug(f'{hex(stringtable_offset)} {hex(nameoffset)}')
        log.debug(f'{hex(datasize)}')

        file = cls(filename, fileid, hashcode, flags)

        f.seek(globaldataoffset+filedataoffset)
        file.write(f.read(datasize))
        DATA[0] += datasize
        # Reset file position
        file.seek(0)

        return file

    def dump(self, f):
        if self.is_yaz0_compressed:
            decompress(self, f, suppress_error=True)
        else:
            f.write(self.getvalue())


class Archive(object):
    def __init__(self):
        self.root = None
        
    @classmethod
    def from_dir(cls, path, follow_symlinks=False):
        arc = cls()
        dir = Directory.from_dir(path, follow_symlinks=follow_symlinks)
        arc.root = dir

        return arc


    @classmethod
    def from_file(cls, f):
        newarc = cls()
        header = f.read(4)

        if header == b"Yaz0":
            # Decompress first
            log.info("Yaz0 header detected, decompressing...")
            start = time.time()
            tmp = BytesIO()
            f.seek(0)
            decompress(f, tmp)
            #with open("decompressed.bin", "wb") as g:
            #    decompress(f,)
            f = tmp
            f.seek(0)

            header = f.read(4)
            log.info("Finished decompression.")
            log.info(f"Time taken: {time.time() - start}")

        if header == b"RARC":
            pass
        else:
            raise RuntimeError("Unknown file header: {} should be Yaz0 or RARC".format(header))

        size = read_uint32(f)
        f.read(4) #unknown

        data_offset = read_uint32(f) + 0x20
        f.read(16) # Unknown
        node_count = read_uint32(f)
        f.read(8) # Unknown
        file_entry_offset = read_uint32(f) + 0x20
        f.read(4) # Unknown
        stringtable_offset = read_uint32(f) + 0x20
        f.read(8) # Unknown
        nodes = []

        log.debug(f"Archive has {node_count} total directories")

                
        
        log.debug(f"data offset {hex(data_offset)}")
        for i in range(node_count):
            nodetype = f.read(4)
            nodedata = f.read(4+2+2+4)
            nameoffset, unknown, entrycount, entryoffset = unpack(">IHHI", nodedata)

            if i == 0:
                dir_name = stringtable_get_name(f, stringtable_offset, nameoffset)
            else:
                dir_name = None 
                
            nodes.append((dir_name, unknown, entrycount, entryoffset))

        rootfoldername = nodes[0][0]
        newarc.root = Directory.from_node(f, rootfoldername, stringtable_offset, file_entry_offset, data_offset, nodes, 0)
        
        return newarc


    def listdir(self, path):
        if path == ".":
            return [self.root.name]
        else:
            dir = self[path]
            entries = []
            entries.extend(dir.files.keys())
            entries.extend(dir.subdirs.keys())
            return entries

    def __getitem__(self, path):
        dirname, rest = split_path(path)

        if rest is None or rest.strip() == "":
            if dirname != self.root.name:
                raise FileNotFoundError(f'Unable to find "{path}" in ARC file')
            else:
                return self.root
        else:
            return self.root[rest]

    def __setitem__(self, path, entry):
        dirname, rest = split_path(path)

        if rest is None or rest.strip() == "":
            if dirname != self.root.name:
                raise RuntimeError("Cannot have more than one directory in the root.")
            elif isinstance(entry, Directory):
                self.root = entry
            else:
                raise TypeError("Root entry should be of type directory but is type '{}'".format(type(entry)))
        else:
            self.root[rest] = entry

    def extract_to(self, path):
        self.root.extract_to(path)

    def write_arc_compressed(self, f, compression_settings, filelisting = None, maxindex = 0):
        temp = BytesIO()
        self.write_arc(temp, compression_settings, filelisting, maxindex)
        temp.seek(0)
        
        if compression_settings.yaz0_fast:
            compress_fast(temp, f)
        elif compression_settings.wszst:
            data = compression_settings.run_wszst(temp)
        
            f.write(data)
    
    def write_arc_uncompressed(self, f, filelisting=None, maxindex=0):
        self.write_arc(f, CompressionSetting())
        
    def write_arc(self, f, compression_settings, filelisting=None, maxindex=0):
        
        stringtable = StringTable()

        nodes = BytesIO()
        entries = BytesIO()
        data = BytesIO()

        nodecount = 1
        entries = 0

        # Set up string table with all directory and file names
        stringtable.write_string(".")
        stringtable.write_string("..")
        stringtable.write_string(self.root.name)

        for dir, subdirnames, filenames in self.root.walk():
            nodecount += len(subdirnames)
            entries += len(subdirnames) + len(filenames)

            for name in subdirnames:
                stringtable.write_string(name)

            for name in filenames:
                stringtable.write_string(name)

        f.write(b"RARC")
        f.write(b"FOO ") # placeholder for filesize
        write_uint32(f, 0x20)  #Unknown but often 0x20?
        f.write(b"BAR ") # placeholder for data offset
        f.write(b"\x00"*16) # 4 unknown ints

        write_uint32(f, nodecount)
        write_uint32(f, 0x20) # unknown
        f.write(b"\x00"*4) # 1 unknown ints

        #aligned_file_entry_offset = (0x20 + 44 + (nodecount*16) + 0x1F) & 0x20
        #write_uint32(f, aligned_file_entry_offset)  # Offset to file entries aligned to multiples of 0x20
        write_uint32(f, 0xF0F0F0F0)

        f.write(b"\x00"*4) # 1 unknown int

        #aligned_stringtable_offset = aligned_file_entry_offset + ((entries * 20) + 0x1F) & 0x20
        #write_uint32(f, aligned_stringtable_offset)
        write_uint32(f, 0xF0F0F0F0)

        f.write(b"\x00"*8) # 2 unknown ints

        node_offset = f.tell()

        first_file_entry_index = 0

        dirlist = []

        #aligned_data_offset = aligned_stringtable_offset + (stringtable.size() + 0x1F) & 0x20

        for i, dirinfo in enumerate(self.root.walk()):
            dirpath, dirnames, filenames = dirinfo
            dir = self[dirpath]
            dir._nodeindex = i

            dirlist.append(dir)

            if i == 0:
                nodetype = b"ROOT"
            else:
                nodetype = dir.name.upper().encode("shift-jis")[:4]
                if len(nodetype) < 4:
                    nodetype = nodetype + (b"\x00"*(4 - len(nodetype)))

            f.write(nodetype)
            write_uint32(f, stringtable.get_string_offset(dir.name))
            hash = hash_name(dir.name)

            entrycount = len(dirnames) + len(filenames)
            write_uint16(f, hash)
            write_uint16(f, entrycount+2)

            write_uint32(f, first_file_entry_index)
            first_file_entry_index += entrycount + 2 # Each directory has two special entries being the current and the parent directories

        write_pad32(f)

        current_file_entry_offset = f.tell()
        #assert f.tell() == aligned_file_entry_offset
        fileid = maxindex
        
        def key_compare(val):
            #if filelisting is not None:
            #    if val[0] in filelisting:
            #        return filelisting[val[0]][0]
            return maxindex + 1
        
        for dir in dirlist:
            log.debug(f"Hello {dir.absolute_path()}")
            abspath = dir.absolute_path()   
            files = []
            
            for filename, file in dir.files.items():
                files.append((abspath+"/"+filename, file))
            
            files.sort(key=key_compare)            
            
            
            for filepath, file in files:
                filemeta = FileListing.default()
                if filelisting is not None:
                    if filepath in filelisting:
                        fileid, filemeta = filelisting[filepath]
                        write_uint16(f, fileid)
                        log.debug(f"found filemeta")
                    else:
                        write_uint16(f, fileid)
                else:
                    write_uint16(f, fileid)
                filename = file.name 
                write_uint16(f, hash_name(filename))
                log.debug(f"Writing filemeta {str(filemeta)}")
                write_uint8(f, filemeta.to_flags())
                write_uint8(f, 0) # padding 
                #f.write(b"\x11\x00") # Flag for file+padding
                write_uint16(f, stringtable.get_string_offset(filename))

                filedata_offset = data.tell()
                write_uint32(f, filedata_offset) # Write file data offset
                
                if filemeta.is_yaz0 and filemeta.is_compressed:
                    log.debug("so far so gud")
                    if compression_settings.wszst:
                        log.debug("doing wszst thing")
                        compressed_data = compression_setting.run_wszst(file)
                        data.write(compressed_data)
                    else:
                        # if file was yaz0 compressed then always yaz0fast compress even if wszst is not set
                        #yaz0.compress_fast(file, data)
                        data.write(file.getvalue())
                else:
                    data.write(file.getvalue()) # Write file data
                
                
                write_uint32(f, data.tell()-filedata_offset) # Write file size
                write_pad32(data)
                write_uint32(f, 0)

                fileid += 1

            specialdirs = [(".", dir), ("..", dir.parent)]

            for subdirname, subdir in chain(specialdirs, dir.subdirs.items()):
                write_uint16(f, 0xFFFF)
                write_uint16(f, hash_name(subdirname))
                f.write(b"\x02\x00") # Flag for directory+padding
                write_uint16(f, stringtable.get_string_offset(subdirname))

                if subdir is None:
                    child_nodeindex = 0xFFFFFFFF
                else:
                    child_nodeindex = subdir._nodeindex
                write_uint32(f, child_nodeindex)
                write_uint32(f, 0x10)
                write_uint32(f, 0) # Padding

        write_pad32(f)
        assert f.tell() % 0x20 == 0
        current_stringtable_offset = f.tell()
        stringtable.write_to(f)

        write_pad32(f)
        stringtablesize = f.tell() - current_stringtable_offset

        current_data_offset = f.tell()

        f.write(data.getvalue())

        rarc_size = f.tell()

        f.seek(4)
        write_uint32(f, rarc_size)
        f.seek(12)
        write_uint32(f, current_data_offset-0x20)
        write_uint32(f, rarc_size - current_data_offset)
        write_uint32(f, rarc_size - current_data_offset)

        f.seek(40)

        total_file_entries = first_file_entry_index
        write_uint32(f, total_file_entries)
        write_uint32(f, current_file_entry_offset-0x20)
        write_uint32(f, stringtablesize)
        write_uint32(f, current_stringtable_offset-0x20)
















if __name__ == "__main__":
    import argparse
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("input",
                        help="Path to the archive file (usually .arc or .szs) to be extracted or the directory to be packed into an archive file.")
    parser.add_argument("--yaz0fast", action="store_true",
                        help="Encode archive as yaz0 when doing directory->.arc/.szs")
    parser.add_argument("--wszst", action="store_true",
                        help="Use wszst (Wimms SZS tools) for yaz0 compression when doing directory->arc/.szs. wszst needs to be installed separately")
    parser.add_argument("--wszst_comprlevel", default="9",
                        help=("Set the compression level for wszst. Values are the same as in wszst's documentation. "
                        "Possible values are 0..10 with 0 being worst, 9 being the default and best and 10 being ultra and most time consuming."))
    parser.add_argument("output", default=None, nargs = '?',
                        help="Output path to which the archive is extracted or a new archive file is written, depending on input.")

    args = parser.parse_args()

    inputpath = os.path.normpath(args.input)
    if os.path.isdir(inputpath):
        dir2arc = True
    else:
        dir2arc = False

    compression_setting = CompressionSetting(args.yaz0fast, args.wszst, args.wszst_comprlevel)
    log.debug(f"Use wszst? {args.wszst}")
    
    if args.output is None:
        path, name = os.path.split(inputpath)

        if dir2arc:
            if args.yaz0fast:
                ending = ".szs"
            else:
                ending = ".arc"
            
            if inputpath.endswith("_ext"):
                outputpath = inputpath[:-4]
            else:
                outputpath = inputpath + ending 
        else:
            outputpath = os.path.join(path, name+"_ext")
    else:
        outputpath = args.output

    if dir2arc:
        dirscan = os.scandir(inputpath)
        inputdir = None 
        
        for entry in dirscan:
            if entry.is_dir():
                if inputdir is None:
                    inputdir = entry.name
                else:
                    raise RuntimeError("Directory {0} contains multiple folders! Only one folder should exist.".format(inputpath))
        
        if inputdir is None:
            raise RuntimeError("Directory {0} contains no folders! Exactly one folder should exist.".format(inputpath))
        
        log.debug("Packing directory to archive")
        archive = Archive.from_dir(os.path.join(inputpath, inputdir))
        filelisting = {}
        maxindex = 0
        try: 
            with open(os.path.join(inputpath, "filelisting.txt"), "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#"): continue 
                    result = line.split(" ")
                    if len(result) == 2:
                        path, fileid = result 
                        filelisting_meta = FileListing.default()
                    else:
                        path, fileid, metadata = result 
                        filelisting_meta = FileListing.from_string(metadata)
                        log.debug(f"{metadata} {filelisting_meta}")
                    
                    filelisting[path] = (int(fileid), filelisting_meta)
                    if int(fileid) > maxindex:
                        maxindex = int(fileid)
        except:
            log.debug("no filelisting")
            pass
        
        log.debug("Directory loaded into memory, writing archive now")
        
        
        
        with open(outputpath, "wb") as f:
            if args.yaz0fast or args.wszst:
                archive.write_arc_compressed(f, compression_setting, filelisting, maxindex)
            else:
                archive.write_arc(f, compression_setting, filelisting, maxindex)
        log.debug("Done")
    else:
        log.debug("Extracting archive to directory")
        with open(inputpath, "rb") as f:
            archive = Archive.from_file(f)
        archive.extract_to(outputpath)
        
        with open(os.path.join(outputpath, "filelisting.txt"), "w") as f:
            f.write("# DO NOT TOUCH THIS FILE\n")
            for dirpath, dirnames, filenames in archive.root.walk():
                currentdir = archive[dirpath]
                #for name in dirnames:
                #    
                #    dir = currentdir[name]
                #    f.write(dirpath+"/"+name)
                #    f.write("\n")
                    
                for name in filenames:
                    file = currentdir[name]
                    f.write(dirpath+"/"+name)
                    f.write(" ")
                    f.write(str(file._fileid))
                    meta = file.filetype.to_string()
                    log.debug(f"{hex(file._flags)} {file.filetype.to_string()}")
                    if meta:
                        f.write(" ")
                        f.write(meta)
                    f.write("\n")


