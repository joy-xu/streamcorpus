#!/usr/bin/env python
'''
Provides a reader/writer for batches of Thrift messages stored in flat
files.

Defaults to streamcorpus.StreamItem and can be used with any
Thrift-defined objects.

This software is released under an MIT/X11 open source license.

Copyright 2012 Diffeo, Inc.
'''

import logging
logger = logging.getLogger('streamcorpus')

## import the thrift library
from thrift import Thrift
from thrift.transport import TTransport
from thrift.protocol.TBinaryProtocol import TBinaryProtocol, TBinaryProtocolAccelerated
fastbinary_import_failure = None
try:
    from thrift.protocol import fastbinary
    ## use faster C program to read/write
    protocol = TBinaryProtocolAccelerated

except Exception, exc:
    fastbinary_import_failure = exc
    ## fall back to pure python
    protocol = TBinaryProtocol

try:
    from backports import lzma
except:
    lzma = None

import gzip
import os
import uuid
import errno
import shutil
import hashlib
import subprocess
import exceptions
from cStringIO import StringIO

from ttypes import StreamItem as StreamItem_v0_3_0
from ttypes_v0_1_0 import StreamItem as StreamItem_v0_1_0
from ttypes_v0_2_0 import StreamItem as StreamItem_v0_2_0

class VersionMismatchError(Exception):
    pass

def serialize(msg):
    '''
    Generate a serialized binary blob for a single message
    '''
    o_transport = StringIO()
    o_protocol = protocol(o_transport)
    msg.write(o_protocol)
    o_transport.seek(0)
    return o_transport.getvalue()

def deserialize(blob, message=StreamItem_v0_3_0):
    '''
    Generate a msg from a serialized binary blob for a single msg
    '''
    chunk = Chunk(data=blob, message=message)
    mesgs = list(chunk)
    assert len(mesgs) == 1, 'got %d messages to deserialize instead of one: %r' % (
        len(mesgs), mesgs)
    return mesgs[0]

class md5_file(object):
    '''
    Adapter around a filehandle that wraps .read and .write so that it
    can construct an md5_hexdigest property
    '''
    def __init__(self, fh):
        self._fh = fh
        self._md5 = hashlib.md5()
        if hasattr(fh, 'get_value'):
            self.get_value = fh.get_value
        if hasattr(fh, 'seek'):
            self.seek = fh.seek
        if hasattr(fh, 'mode'):
            self.mode = fh.mode
        if hasattr(fh, 'flush'):
            self.flush = fh.flush
        if hasattr(fh, 'close'):
            self.close = fh.close

    def read(self, *args, **kwargs):
        data = self._fh.read(*args, **kwargs)
        self._md5.update(data)
        return data

    def write(self, data, *args, **kwargs):
        self._md5.update(data)
        self._fh.write(data, *args, **kwargs)


    def readAll(self, sz):
        '''
        This method allows TBinaryProtocolAccelerated to actually function.

        Copied from here
        http://svn.apache.org/repos/asf/hive/trunk/service/lib/py/thrift/transport/TTransport.py
        '''
        buff = ''
        have = 0
        while (have < sz):
            chunk = self.read(sz-have)
            have += len(chunk)
            buff += chunk

            if len(chunk) == 0:
                raise EOFError()

        return buff


    @property
    def md5_hexdigest(self):
        return self._md5.hexdigest()

class Chunk(object):
    '''
    reader/writer for batches of Thrift messages stored in flat files.
    '''
    def __init__(self, path=None, data=None, file_obj=None, mode='rb',
                 message=StreamItem_v0_3_0,
                 read_wrapper=None, write_wrapper=None,
        ):
        '''Load a chunk from an existing file handle or buffer of data.
        If no data is passed in, then chunk starts as empty and
        chunk.add(message) can be called to append to it.

        mode is only used if you specify a path to an existing file to
        open.

        :param path: path to a file in the local file system.  If path
        ends in .xz then mode must be 'rb' and the entire file is
        loaded into memory and decompressed before the Chunk is ready
        for reading.

        :param mode: read/write mode for opening the file; if
        mode='wb', then a file will be created.

        :file_obj: already opened file, mode must agree with mode
        parameter.

        :param data: bytes of data from which to read messages

        :param message: defaults to StreamItem_v0_3_0; you can specify
        your own Thrift-generated class here.

        :param read_wrapper: a function that takes a deserialized
        message as input and returns a new object to yield from
        __iter__

        :param write_wrapper: a function used in Chunk.add(obj) that
        takes the added object as input and returns another object
        that is a thrift class that can be serialized.
        '''
        if not fastbinary_import_failure:
            logger.debug('using TBinaryProtocolAccelerated (fastbinary)')

        else:
            logger.warn('import fastbinary failed; falling back to 15x slower TBinaryProtocol: %r'\
                            % fastbinary_import_failure)

        self.read_wrapper = read_wrapper
        self.write_wrapper = write_wrapper

        allowed_modes = ['wb', 'ab', 'rb']
        assert mode in allowed_modes, 'mode=%r not in %r' % (mode, allowed_modes)
        self.mode = mode

        ## class for constructing messages when reading
        self.message = message

        ## initialize internal state before figuring out what data we
        ## are acting on
        self._count = 0
        self._md5_hexdigest = None

        ## might not have any output parts
        self._o_chunk_fh = None
        self._o_transport = None
        self._o_protocol = None

        ## might not have any input parts
        self._i_chunk_fh = None
        self._i_transport = None
        self._i_protocol = None

        ## open an existing file from path, or create it
        if path is not None:
            assert data is None and file_obj is None, \
                'Must specify only path or data or file_obj'
            if os.path.exists(path):
                ## if the file is there, then use mode 
                if mode not in ['rb', 'ab']:
                    exc = IOError('mode=%r would overwrite existing %s' % (mode, path))
                    exc.errno = errno.EEXIST
                    raise exc
                if path.endswith('.xz'):
                    assert mode == 'rb', 'mode=%r for .xz' % mode
                    ## launch xz child
                    xz_child = subprocess.Popen(
                        ['xzcat', path],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE)
                    file_obj = xz_child.stdout
                    ## what to do with stderr
                elif path.endswith('.gz'):
                    assert mode == 'rb', 'mode=%r for .gz' % mode
                    file_obj  = gzip.open(path)
                elif path.endswith('.xz.gpg'):
                    assert mode == 'rb', 'mode=%r for .xz' % mode
                    ## launch xz child
                    xz_child = subprocess.Popen(
                        ['gpg -d %s | xz --decompress' % path],
                        stdout=subprocess.PIPE, shell=True)
                        #stderr=subprocess.PIPE)
                    file_obj = xz_child.stdout
                    ## what to do with stderr?
                else:
                    file_obj = open(path, mode)
            else:
                ## otherwise make one for writing
                if mode not in ['wb', 'ab']:
                    exc = IOError('%s does not exist but mode=%r' % (path, mode))
                    exc.errno = errno.ENOENT
                    raise exc
                dirname = os.path.dirname(path)
                if dirname and not os.path.exists(dirname):
                    os.makedirs(dirname)
                if path.endswith('.gz'):
                    file_obj = gzip.open(path, mode)
                elif path.endswith('.xz'):
                    if lzma is None:
                        raise Exception('file extension is .xz but backports.lzma is not installed')
                    file_obj = lzma.open(path, mode)
                else:
                    file_obj = open(path, mode)

        ## if created without any arguments, then prepare to add
        ## messages to an in-memory file object
        if data is None and file_obj is None:
            ## make the default behavior when instantiated as Chunk()
            ## to write to an in-memory buffer
            file_obj = StringIO()
            self.mode = 'wb'
            mode = self.mode

        elif file_obj is None: ## --> must have 'data'
            ## wrap the data in a file obj for reading
            if mode == 'rb':
                file_obj = StringIO(data)
                file_obj.seek(0)
            elif mode == 'ab':
                file_obj = StringIO()
                file_obj.write(data)
                ## and let it just keep writing to it
            else:
                raise Exception('mode=%r but specified "data"' % mode)

        elif file_obj is not None and hasattr(file_obj, 'mode'):
            if isinstance(file_obj.mode, int):
                ## some tools, like python gzip library, use int modes
                file_obj_mode = {1: 'r', 2: 'w'}[file_obj.mode]
            else:
                file_obj_mode = file_obj.mode

            assert file_obj_mode[0] == mode[0], 'file_obj.mode=%r != %r=mode'\
                % (file_obj_mode, mode)
            ## use the file object for writing out the data as it
            ## happens, i.e. in streaming mode.

        if mode in ['ab', 'wb']:
            self._o_chunk_fh = md5_file( file_obj )
            self._o_transport = TTransport.TBufferedTransport(self._o_chunk_fh)
            self._o_protocol = protocol(self._o_transport)

        else:
            assert mode == 'rb', mode
            self._i_chunk_fh = md5_file( file_obj )
            #_i_transport and _i_protocol are set below in __iter__

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()

    def add(self, msg):
        'add message instance to chunk'
        assert self._o_protocol, 'cannot add to a Chunk instantiated with data'
        assert self._o_chunk_fh is not None, 'cannot Chunk.add after Chunk.close'
        if self.write_wrapper is not None:
            msg = self.write_wrapper(msg)
        if not (isinstance(msg, self.message) or (type(msg) == self.message)):
            raise VersionMismatchError(
                'mismatched type: %s != %s' % (type(msg), self.message))
        msg.write(self._o_protocol)
        self._count += 1

    def flush(self):
        if self._o_chunk_fh is not None:
            self._o_transport.flush()

    def close(self):
        '''
        Close any chunk file that we might have had open for writing.
        '''
        if self._o_chunk_fh is not None:
            self._o_transport.flush()
            self._o_chunk_fh.close()
            ## make this method idempotent
            self._md5_hexdigest = self._o_chunk_fh.md5_hexdigest
            self._o_chunk_fh = None

    @property
    def md5_hexdigest(self):
        if self._md5_hexdigest:
            ## only set if closed already
            return self._md5_hexdigest
        if self._o_chunk_fh:
            ## get it directly from the output chunk
            return self._o_chunk_fh.md5_hexdigest
        elif self._i_chunk_fh:
            ## get it directly from the input chunk
            return self._i_chunk_fh.md5_hexdigest
        else:
            ## maybe return raise?
            return None

    def __str__(self):
        raise exceptions.NotImplementedError

    def __repr__(self):
        return 'Chunk(len=%d)' % len(self)

    def __len__(self):
        ## how to make this pythonic given that we have __iter__?
        return self._count

    def __iter__(self):
        '''
        Iterator over messages in the chunk
        '''
        assert self._i_chunk_fh, 'cannot iterate over a Chunk open for writing'

        ## attempt to seek to the start, so can iterate multiple times
        ## over the chunk
        if hasattr(self._i_chunk_fh, 'seek'):
            try:
                self._i_chunk_fh.seek(0)
            except IOError:
                pass
                ## just assume that it is a pipe like stdin that need
                ## not be seeked to start

        ## wrap the file handle in buffered transport
        i_transport = TTransport.TBufferedTransport(self._i_chunk_fh)
        ## use the Thrift Binary Protocol
        i_protocol = protocol(i_transport)

        ## read message instances until input buffer is exhausted
        while 1:

            ## instantiate a message  instance 
            msg = self.message()

            try:
                ## read it from the thrift protocol instance
                msg.read(i_protocol)

                if hasattr(msg, 'version'):
                    ## compare the read version to the default version
                    ## value on the identified message
                    if not (msg.version == self.message().version):
                        raise VersionMismatchError(
                            'read msg.version = %d != %d = message().version):' % \
                                (msg.version, self.message().version))
                
                ## yield is python primitive for iteration
                self._count += 1

                if self.read_wrapper is not None:
                    msg = self.read_wrapper(msg)
                yield msg

            except EOFError:
                break

def decrypt_and_uncompress(data, gpg_private=None, tmp_dir='/tmp'):
    '''
    Given a data buffer of bytes, if gpg_key_path is provided, decrypt
    data using gnupg, and uncompress using xz.

    :returns (logs, data): where logs is an array of strings, and data
    is a binary string.
    '''
    _errors = []
    tmp_path = os.path.join(tmp_dir, 'tmp-compress-and-encrypt-path-' + uuid.uuid4().hex)
    if not os.path.exists(tmp_path):
        os.makedirs(tmp_path)
    if gpg_private is not None:
        ### setup gpg for decryption
        gpg_dir = os.path.join(tmp_path, 'gpg_dir')
        os.makedirs(gpg_dir)

        gpg_child = subprocess.Popen(
            ['gpg', '--no-permission-warning', '--homedir', gpg_dir,
             '--import', gpg_private],
            stderr=subprocess.PIPE)
        s_out, errors = gpg_child.communicate()
        if errors:
            _errors.append('gpg logs to stderr, read carefully:\n\n%s' % errors)

        ## decrypt it, and free memory
        ## encrypt using the fingerprint for our trec-kba-rsa key pair
        gpg_child = subprocess.Popen(
            ## setup gpg to decrypt with trec-kba private key
            ## (i.e. make it the recipient), with zero compression,
            ## ascii armoring is off by default, and --output - must
            ## appear before --decrypt -
            ['gpg',   '--no-permission-warning', '--homedir', gpg_dir,
             '--trust-model', 'always', '--output', '-', '--decrypt', '-'],
            stdin =subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        ## communicate with child via its stdin 
        data, errors = gpg_child.communicate(data)
        if errors:
            _errors.append(errors)

        ## remove the gpg_dir
        shutil.rmtree(gpg_dir, ignore_errors=True)

    ## launch xz child
    xz_child = subprocess.Popen(
        ['xz', '--decompress'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)
    ## use communicate to pass the data incrementally to the child
    ## while reading the output, to avoid blocking 
    data, errors = xz_child.communicate(data)

    assert not errors, errors

    return _errors, data

def compress_and_encrypt(data, gpg_public=None, gpg_recipient='trec-kba'):
    '''
    Given a data buffer of bytes compress it using xz, if gpg_public
    is provided, encrypt data using gnupg.
    '''
    _errors = []
    ## launch xz child
    xz_child = subprocess.Popen(
        ['xz', '--compress'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)
    ## use communicate to pass the data incrementally to the child
    ## while reading the output, to avoid blocking 
    data, errors = xz_child.communicate(data)

    assert not errors, errors

    if gpg_public is not None:
        ### setup gpg for encryption.  
        gpg_dir = os.path.join(tmp_dir, 'tmp-compress-and-encrypt-' + uuid.uuid1().hex)
        os.makedirs(gpg_dir)

        ## Load public key.  Could do this just once, but performance
        ## hit is minor and code simpler to do it everytime
        gpg_child = subprocess.Popen(
            ['gpg', '--no-permission-warning', '--homedir', gpg_dir,
             '--import', gpg_public],
            stderr=subprocess.PIPE)
        s_out, errors = gpg_child.communicate()
        if errors:
            _errors.append('gpg logs to stderr, read carefully:\n\n%s' % errors)

        ## encrypt using the fingerprint for our trec-kba-rsa key pair
        gpg_child = subprocess.Popen(
            ## setup gpg to decrypt with trec-kba private key
            ## (i.e. make it the recipient), with zero compression,
            ## ascii armoring is off by default, and --output - must
            ## appear before --encrypt -
            ['gpg',  '--no-permission-warning', '--homedir', gpg_dir,
             '-r', gpg_recipient, '-z', '0', '--trust-model', 'always',
             '--output', '-', '--encrypt', '-'],
            stdin =subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        ## communicate with child via its stdin 
        data, errors = gpg_child.communicate(data)
        if errors:
            _errors.append(errors)

        shutil.rmtree(gpg_dir, ignore_errors=True)

    return _errors, data

def compress_and_encrypt_path(path, gpg_public=None, gpg_recipient='trec-kba', tmp_dir='/tmp'):
    '''
    Given a path in the local file system, compress it using xz, if gpg_public
    is provided, encrypt data using gnupg.

    :returns: path to file of encrypted, compressed data
    :rtype: str
    '''
    _errors = []
    assert os.path.exists(path), path
    command = 'xz --compress < ' + path

    tmp_path = os.path.join(tmp_dir, 'tmp-compress-and-encrypt-path-' + uuid.uuid4().hex)
    if not os.path.exists(tmp_path):
        os.makedirs(tmp_path)

    if gpg_public is not None:
        ### setup gpg for encryption.  
        gpg_dir = os.path.join(tmp_path, 'gpg_dir')
        os.makedirs(gpg_dir)

        ## Load public key.  Could do this just once, but performance
        ## hit is minor and code simpler to do it everytime
        gpg_child = subprocess.Popen(
            ['gpg', '--no-permission-warning', '--homedir', gpg_dir,
             '--import', gpg_public],
            stderr=subprocess.PIPE)
        s_out, errors = gpg_child.communicate()
        if errors:
            _errors.append('gpg logs to stderr, read carefully:\n\n%s' % errors)

        ## setup gpg to decrypt with provided private key (i.e. make
        ## it the recipient), with zero compression, ascii armoring is
        ## off by default, and --output - must come before --encrypt -
        command += '| gpg  --no-permission-warning --homedir ' + gpg_dir \
                 + ' -r ' + gpg_recipient \
                 + ' -z 0 --trust-model always --output - --encrypt - '

    ## we want to capture any errors, so do all the work before
    ## returning.  Store the intermediate result in this temp file:
    o_path = os.path.join(tmp_path, 'o_path')
    e_path = os.path.join(tmp_path, 'e_path')

    command += ' 1> ' + o_path 

    ## wrap entire command in parentheses and pipe stderr to a file
    command = '( ' + command + ') 2> ' + e_path

    ## launch xz child
    child = os.system(command)

    if os.path.exists(e_path):
        errors = open(e_path).read()
        ## clean up temp path
        os.remove(e_path)
    else:
        errors = None

    if errors:
        ## will send back errors in list
        _errors.append(errors)

    if gpg_public is not None:
        shutil.rmtree(gpg_dir, ignore_errors=True)

    return _errors, o_path
