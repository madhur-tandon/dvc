import os
import shutil
from typing import IO, TYPE_CHECKING, Any, Dict, Iterator, Optional, overload

from funcy import cached_property
from tqdm.utils import CallbackIOWrapper

from dvc.progress import DEFAULT_CALLBACK

from .base import BaseFileSystem

FSPath = str
AnyFSPath = str

if TYPE_CHECKING:
    from typing import Iterable

    from typing_extensions import Literal


# An info() entry, might evolve to a TypedDict
# in the future (e.g for properly type 'size' etc).
Entry = Dict[str, Any]


# pylint: disable=no-member
class FSSpecWrapper(BaseFileSystem):
    TRAVERSE_PREFIX_LEN = 2

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fs_args = {"skip_instance_cache": True}
        self.fs_args.update(self._prepare_credentials(**kwargs))

    @staticmethod
    def _get_kwargs_from_urls(urlpath: str) -> "Dict[str, Any]":
        from fsspec.utils import infer_storage_options

        options = infer_storage_options(urlpath)
        options.pop("path", None)
        options.pop("protocol", None)
        return options

    @cached_property
    def fs(self):
        raise NotImplementedError

    def _prepare_credentials(
        self, **config: Dict[str, Any]  # pylint: disable=unused-argument
    ) -> Dict[str, Any]:
        """Prepare the arguments for authentication to the
        host filesystem"""
        return {}

    def _isdir(self, path_info: AnyFSPath) -> bool:
        return self.fs.isdir(path_info)

    def isdir(self, path_info: AnyFSPath) -> bool:
        try:
            return self._isdir(path_info)
        except FileNotFoundError:
            return False

    def isfile(self, path_info: AnyFSPath) -> bool:
        try:
            return not self._isdir(path_info)
        except FileNotFoundError:
            return False

    def is_empty(self, path_info: AnyFSPath) -> bool:
        entry = self.info(path_info)
        if entry["type"] == "directory":
            return not self.fs.ls(path_info)
        return entry["size"] == 0

    def open(
        self,
        path_info: AnyFSPath,
        mode: str = "r",
        encoding: Optional[str] = None,
        **kwargs,
    ) -> "IO":  # pylint: disable=arguments-differ
        return self.fs.open(path_info, mode=mode)

    def checksum(self, path_info: AnyFSPath) -> str:
        return self.fs.checksum(path_info)

    def copy(self, from_info: AnyFSPath, to_info: AnyFSPath) -> None:
        self.makedirs(self.path.parent(to_info))
        self.fs.copy(from_info, to_info)

    def exists(self, path_info: AnyFSPath) -> bool:
        return self.fs.exists(path_info)

    @overload
    def ls(
        self, path_info: AnyFSPath, detail: "Literal[True]"
    ) -> "Iterator[Entry]":
        ...

    @overload
    def ls(
        self, path_info: AnyFSPath, detail: "Literal[False]"
    ) -> Iterator[str]:
        ...

    def ls(self, path_info, detail=False):
        yield from self.fs.ls(path_info, detail=detail)

    @overload  # type: ignore
    def find(
        self,
        path_info: AnyFSPath,
        detail: "Literal[True]",
        prefix: Optional[bool],
    ) -> "Iterator[Entry]":
        ...

    @overload
    def find(
        self,
        path_info: AnyFSPath,
        detail: "Literal[False]",
        prefix: Optional[bool],
    ) -> Iterator[str]:
        ...

    # pylint: disable=unused-argument
    def find(self, path_info, detail=False, prefix=None):
        yield from self.fs.find(path_info, detail=detail)

    def move(self, from_info: AnyFSPath, to_info: AnyFSPath) -> None:
        self.fs.move(from_info, to_info)

    def remove(self, path_info: AnyFSPath) -> None:
        self.fs.rm_file(path_info)

    def info(self, path_info: AnyFSPath) -> "Entry":
        return self.fs.info(path_info)

    def makedirs(self, path_info: AnyFSPath, **kwargs) -> None:
        self.fs.makedirs(
            path_info, exist_ok=kwargs.pop("exist_ok", True)
        )

    def put_file(
        self,
        from_file: AnyFSPath,
        to_info: AnyFSPath,
        callback: Any = DEFAULT_CALLBACK,
        **kwargs,
    ) -> None:
        self.fs.put_file(
            from_file, to_info, callback=callback, **kwargs
        )
        self.fs.invalidate_cache(self.path.parent(to_info))

    def get_file(
        self,
        from_info: AnyFSPath,
        to_info: AnyFSPath,
        callback: Any = DEFAULT_CALLBACK,
        **kwargs,
    ) -> None:
        self.fs.get_file(
            from_info, to_info, callback=callback, **kwargs
        )

    def upload_fobj(self, fobj: IO, to_info: AnyFSPath, **kwargs) -> None:
        self.makedirs(self.path.parent(to_info))
        with self.open(to_info, "wb") as fdest:
            shutil.copyfileobj(
                fobj, fdest, length=getattr(fdest, "blocksize", None)
            )


# pylint: disable=abstract-method
class ObjectFSWrapper(FSSpecWrapper):
    TRAVERSE_PREFIX_LEN = 3

    def makedirs(self, path_info: AnyFSPath, **kwargs) -> None:
        # For object storages make this method a no-op. The original
        # fs.makedirs() method will only check if the bucket exists
        # and create if it doesn't though we don't want to support
        # that behavior, and the check will cost some time so we'll
        # simply ignore all mkdir()/makedirs() calls.
        return None

    def _isdir(self, path_info: AnyFSPath) -> bool:
        # Directory in object storages are interpreted differently
        # among different fsspec providers, so this logic is a temporary
        # measure for us to adapt as of now. It checks whether it is a
        # directory (as in a prefix with contents) or whether it is an empty
        # file where it's name ends with a forward slash

        entry = self.info(path_info)
        return entry["type"] == "directory" or (
            entry["size"] == 0
            and entry["type"] == "file"
            and entry["name"].endswith("/")
        )

    def find(self, path_info, detail=False, prefix=None):
        if prefix:
            path = self.path.parent(path_info)
            files = self.fs.find(
                path, detail=detail, prefix=self.path.parts(path_info)[-1]
            )
        else:
            files = self.fs.find(path_info, detail=detail)

        if detail:
            files = list(files.values())

        # When calling find() on a file, it returns the same file in a list.
        # For object-based storages, the same behavior applies to empty
        # directories since they are represented as files. This condition
        # checks whether we should yield an empty list (if it is an empty
        # directory) or just yield the file itself.
        if len(files) == 1 and files[0] == path and self.isdir(path_info):
            return None

        yield from files


# pylint: disable=arguments-differ
class NoDirectoriesMixin:
    def isdir(self, *args, **kwargs):
        return False

    def isfile(self, *args, **kwargs):
        return True

    def find(self, *args, **kwargs):
        raise NotImplementedError

    def walk(self, *args, **kwargs):
        raise NotImplementedError

    def ls(self, *args, **kwargs):
        raise NotImplementedError


class CallbackMixin:
    """Provides callback support for the filesystem that don't support yet."""

    def put_file(
        self,
        from_file,
        to_info,
        callback=DEFAULT_CALLBACK,
        **kwargs,
    ):
        """Add compatibility support for Callback."""
        # pylint: disable=protected-access
        self.makedirs(self.path.parent(to_info))
        size = os.path.getsize(from_file)
        with open(from_file, "rb") as fobj:
            callback.set_size(size)
            wrapped = CallbackIOWrapper(callback.relative_update, fobj)
            self.upload_fobj(wrapped, to_info)
            self.fs.invalidate_cache(
                self.path.parent(to_info)
            )

    def get_file(
        self,
        from_info,
        to_info,
        callback=DEFAULT_CALLBACK,
        **kwargs,
    ):
        # pylint: disable=protected-access
        total: int = self.getsize(from_info)
        if total:
            callback.set_size(total)

        with self.open(from_info, "rb") as fobj, open(to_info, "wb") as fdest:
            wrapped = CallbackIOWrapper(callback.relative_update, fobj)
            shutil.copyfileobj(wrapped, fdest, length=fobj.blocksize)
