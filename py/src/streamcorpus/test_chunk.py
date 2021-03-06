import os
import uuid
import time
import errno
import shutil
import pytest
from cStringIO import StringIO
import logging
## utility for tests that configures logging in roughly the same way
## that a program calling bigtree should setup logging
logger = logging.getLogger('streamcorpus')
logger.setLevel( logging.DEBUG )
ch = logging.StreamHandler()
ch.setLevel( logging.DEBUG )
formatter = logging.Formatter('%(asctime)s %(process)d %(levelname)s: %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)

## import from inside the local package, i.e. get these things through __init__.py
from . import make_stream_item, ContentItem, Chunk, serialize, deserialize, compress_and_encrypt_path
from . import VersionMismatchError
from . import Versions
from . import StreamItem_v0_2_0, StreamItem_v0_3_0
from streamcorpus import _chunk

TEST_XZ_PATH = os.path.join(os.path.dirname(__file__), '../../../test-data/john-smith-tagged-by-lingpipe-0-v0_2_0.sc.xz')
TEST_SC_PATH = os.path.join(os.path.dirname(__file__), '../../../test-data/john-smith-tagged-by-lingpipe-0-v0_2_0.sc')

def make_si():
    si = make_stream_item( None, 'http://example.com' )
    si.body = ContentItem(raw='hello!')
    return si

def test_version():
    si = make_si()
    assert si.version == Versions.v0_3_0

def test_v0_2_0():
    for si in Chunk(TEST_SC_PATH, message=StreamItem_v0_2_0):
        assert si.version == Versions.v0_2_0

    with pytest.raises(VersionMismatchError):
        for si in Chunk(TEST_SC_PATH, message=StreamItem_v0_3_0):
            pass

def test_chunk():
    ## write in-memory
    ch = Chunk()
    assert ch.mode == 'wb'
    si = make_si()
    ch.add( si )
    assert len(ch) == 1

def test_chunk_wrapper():
    ## write in-memory
    fh = StringIO()
    ch = Chunk(file_obj=fh, write_wrapper=lambda x: x['dog'], mode='wb')
    assert ch.mode == 'wb'
    si = make_si()
    si = dict(dog=si)
    ch.add( si )
    assert len(ch) == 1
    ch.flush()
    blob = fh.getvalue()
    assert blob
    fh = StringIO(blob)
    ch = Chunk(file_obj=fh, read_wrapper=lambda x: dict(dog=x), mode='rb')
    si2 = list(ch)[0]
    assert si2 == si

def test_xz():
    count = 0
    for si in Chunk(TEST_XZ_PATH, message=StreamItem_v0_2_0):
        count += 1
        assert si.body.clean_visible
    assert count == 197

def test_gz():
    count = 0
    test_gz_path = '/tmp/test_gz_path.gz'
    cmd = 'cat %s | xz --decompress | gzip -9 > %s' % (TEST_XZ_PATH, test_gz_path)
    os.system(cmd)
    ## hinted by ".gz"
    for si in Chunk(test_gz_path, message=StreamItem_v0_2_0):
        count += 1
        assert si.body.clean_visible
    assert count == 197
    os.system('rm %s' % test_gz_path)

@pytest.mark.skipif('not _chunk.lzma')
def test_xz_write():
    count = 0
    test_xz_path = '/tmp/test_path.xz'
    ## hinted by ".xz"
    o_chunk = Chunk(test_xz_path, mode='wb')
    o_chunk.add(make_si())
    o_chunk.close()
    assert len(list(Chunk(test_xz_path))) == 1
    os.system('rm %s' % test_xz_path)

def test_speed():
    count = 0
    start_time = time.time()
    for si in Chunk(TEST_SC_PATH, message=StreamItem_v0_2_0):
        count += 1
        assert si.body.clean_visible
    elapsed = time.time() - start_time
    rate = float(count) / elapsed
    print '%d in %.3f sec --> %.3f per sec' % (count, elapsed, rate)
    assert count == 197


@pytest.fixture(scope='function')
def path(request):
    path = '/tmp/test_chunk-%s.sc' % str(uuid.uuid4())
    def fin():
        os.remove(path)
    request.addfinalizer(fin)
    return path

def test_chunk_path_write(path):
    ## write to path
    ch = Chunk(path=path, mode='wb')
    si = make_si()
    ch.add( si )
    ch.close()
    assert len(ch) == 1
    print repr(ch)
    assert len(list( Chunk(path=path, mode='rb') )) == 1

def test_chunk_path_append(path):
    ch = Chunk(path=path, mode='wb')
    si = make_si()
    ch.add( si )
    ch.close()
    assert len(ch) == 1
    ## append to path
    ch = Chunk(path=path, mode='ab')
    si = make_si()
    ch.add( si )
    ch.close()
    ## count is only for those added
    assert len(ch) == 1
    print repr(ch)
    assert len(list( Chunk(path=path, mode='rb') )) == 2

def test_chunk_path_read_1(path):
    ch = Chunk(path=path, mode='wb')
    ch.add( make_si() )
    ch.add( make_si() )
    ch.close()
    assert len(ch) == 2
    ## read from path
    ch = Chunk(path=path, mode='rb')
    assert len(list(ch)) == 2
    ## updated by __iter__
    assert len(ch) == 2
    print repr(ch)

def test_chunk_path_read_version_protection(path):
    ch = Chunk(path=path, mode='wb')
    ch.add( make_si() )
    ch.add( make_si() )
    ch.close()
    assert len(ch) == 2
    ## read from path
    with pytest.raises(VersionMismatchError):
        for si in Chunk(path=path, mode='rb', message=StreamItem_v0_2_0):
            pass

def test_chunk_data_read_2(path):
    ch = Chunk(path=path, mode='wb')
    ch.add( make_si() )
    ch.add( make_si() )
    ch.close()
    assert len(ch) == 2
    ## load from data
    data = open(path).read()
    ch = Chunk(data=data, mode='rb')
    assert len(list(ch)) == 2
    ## updated by __iter__
    assert len(ch) == 2
    print repr(ch)

def test_chunk_data_append(path):
    ch = Chunk(path=path, mode='wb')
    ch.add( make_si() )
    ch.add( make_si() )
    ch.close()
    assert len(ch) == 2
    ## load from data
    data = open(path).read()
    ch = Chunk(data=data, mode='ab')
    si = make_si()
    ch.add( si )
    assert len(ch) == 1
    print repr(ch)

def test_serialize():
    si = make_si()
    blob = serialize(si)
    si2 = deserialize(blob)
    assert si.stream_id == si2.stream_id

def test_noexists_exception():
    with pytest.raises(IOError) as excinfo:
        Chunk('path-that-does-not-exist', mode='rb')
    assert excinfo.value.errno == errno.ENOENT

def test_exists_exception(path):
    ch = Chunk(path=path, mode='wb')
    ch.add( make_si() )
    ch.add( make_si() )
    ch.close()
    with pytest.raises(IOError) as excinfo:
        Chunk(path, mode='wb')
    assert excinfo.value.errno == errno.EEXIST

def test_compress_and_encrypt_path(path):
    ch = Chunk(path=path, mode='wb')
    ch.add( make_si() )
    ch.add( make_si() )
    ch.close()
    originalSize = os.path.getsize(path)
    tmp_dir = os.path.join('/tmp', uuid.uuid4().hex)
    errors, o_path = compress_and_encrypt_path(path, tmp_dir=tmp_dir)
    if errors:
        print '\n'.join(errors)
        raise Exception(errors)
    #assert len(open(o_path).read()) in [240, 244, 248]
    print 'path=%r o_path=%r size=%s' % (path, o_path, os.path.getsize(o_path))
    newSize = os.path.getsize(o_path)
    assert newSize < originalSize
    assert (newSize % 4) == 0

    ## this should go in a "cleanup" method...
    shutil.rmtree(tmp_dir)

def test_with(path):
    with Chunk(path, mode='wb') as ch:
        ch.add(make_si())
        ch.add(make_si())
        ch.add(make_si())
    assert len(list(Chunk(path))) == 3
