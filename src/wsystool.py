"""
A wrapper for the WSYSTool.
"""
import contextlib
import os
import pathlib
import shlex
import subprocess
import tempfile
import logging

log = logging.getLogger(__name__)


@contextlib.contextmanager
def current_directory(dirpath: str):
    cwd = os.getcwd()
    try:
        os.chdir(dirpath)
        yield
    finally:
        os.chdir(cwd)


def get_wsystool_root() -> str:
    tools_dirpath = str(pathlib.Path(__file__).parent.absolute() / 'tools')
    return os.path.join(tools_dirpath, 'wsystool')


def check_wsystool() -> bool:
    wsystool_root = get_wsystool_root()
    if not os.path.exists(wsystool_root):
        return False
    names = os.listdir(wsystool_root)
    return 'wsystool' in names or 'wsystool.exe' in names


def run(args: list[str]) -> str:
    try:
        return subprocess.run(args,
                              check=True,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT,
                              text=True).stdout
    except subprocess.CalledProcessError as e:
        command = " ".join([shlex.quote(arg) for arg in e.cmd])
        raise RuntimeError(f'Command:\n\n{command}\n\n'
                           f'Error code: {e.returncode}\n\n'
                           f'Output:\n\n{e.output}') from e


def compile_and_install_wsystool():
    WSYSTOOL_GIT_URL = 'https://github.com/XAYRGA/wsystool.git'
    WSYSTOOL_GIT_SHA = '500ee9411dc998cd1e91036af06a3d9d79359867'

    with tempfile.TemporaryDirectory(prefix='mkddpatcher_') as tmp_dir:
        with current_directory(tmp_dir):
            log.info('Checking out WSYSTool...')
            run(('git', 'clone', WSYSTOOL_GIT_URL))

            with current_directory('wsystool'):
                run(('git', 'checkout', WSYSTOOL_GIT_SHA))

                # FIXME(CA): Once the fix is applied upstream (it's been reported), this patch can
                # be discarded.
                with open('wsystool/WSYSProjectDeserializer.cs', 'r', encoding='utf-8') as f:
                    text = f.read()
                text = text.replace(r'}\\{', '}/{')
                with open('wsystool/WSYSProjectDeserializer.cs', 'w', encoding='utf-8') as f:
                    f.write(text)

                log.info('Compiling WSYSTool...')
                run((
                    'dotnet',
                    'build',
                    'wsystool.sln',
                    '--configuration',
                    'Release',
                    '--output',
                    get_wsystool_root(),
                ))

    assert check_wsystool(), 'Tool should be available after successful installation'

    log.info(f'WSYSTool installed successfully in "{get_wsystool_root()}"')


def unpack_wsys(src_filepath: str, dst_dirpath, awpath: str):
    run((
        os.path.join(get_wsystool_root(), 'wsystool'),
        'unpack',
        src_filepath,
        dst_dirpath,
        '-awpath',
        awpath,
    ))


def pack_wsys(src_dirpath: str, dst_filepath: str, awpath: str):
    run((
        os.path.join(get_wsystool_root(), 'wsystool'),
        'pack',
        src_dirpath,
        dst_filepath,
        '-awpath',
        awpath,
    ))
