import contextlib
import sys
import textwrap
import warnings
from importlib.abc import MetaPathFinder
from importlib.machinery import SourceFileLoader
from importlib.util import spec_from_loader
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Union

import vvm
import vyper
import vyper.ir.compile_ir as compile_ir
from packaging.version import Version
from vvm.utils.versioning import _pick_vyper_version, detect_version_specifier_set
from vyper.ast.parse import parse_to_ast
from vyper.cli.vyper_compile import get_search_paths
from vyper.compiler.input_bundle import CompilerInput, FileInput, FilesystemInputBundle
from vyper.compiler.phases import CompilerData
from vyper.compiler.settings import Settings, anchor_settings
from vyper.semantics.analysis.imports import resolve_imports
from vyper.semantics.analysis.module import analyze_module
from vyper.semantics.types.module import ModuleT
from vyper.utils import sha256sum

from boa.contracts.abi.abi_contract import ABIContractFactory
from boa.contracts.vvm.vvm_contract import VVMDeployer
from boa.contracts.vyper.vyper_contract import (
    VyperBlueprint,
    VyperContract,
    VyperDeployer,
)
from boa.environment import Env
from boa.explorer import Etherscan, get_etherscan
from boa.rpc import json
from boa.util.abi import Address
from boa.util.disk_cache import DiskCache

if TYPE_CHECKING:
    from vyper.semantics.analysis.base import ImportInfo

_Contract = Union[VyperContract, VyperBlueprint]


_disk_cache = None
_search_path = None


def set_search_paths(path: list[str]):
    global _search_path
    _search_path = path


def set_search_path(paths: list[str]):
    warnings.warn(
        DeprecationWarning("set_search_path is deprecated, use set_search_paths.")
    )
    set_search_paths(paths)


def set_cache_dir(cache_dir="~/.cache/titanoboa"):
    global _disk_cache
    if cache_dir is None:
        _disk_cache = None
        return
    compiler_version = f"{vyper.__version__}.{vyper.__commit__}"
    _disk_cache = DiskCache(cache_dir, compiler_version)


def disable_cache():
    set_cache_dir(None)


set_cache_dir()  # enable caching, by default!


class BoaImporter(MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        path = Path(fullname.replace(".", "/")).with_suffix(".vy")

        for prefix in sys.path:
            to_try = Path(prefix) / path

            if to_try.exists():
                loader = BoaLoader(fullname, str(to_try))
                return spec_from_loader(fullname, loader)


class BoaLoader(SourceFileLoader):
    def get_code(self, fullname):
        # importlib docs say to return None, but that triggers an `ImportError`
        return ""

    def create_module(self, spec):
        ret = load_partial(self.path)

        # comply with PEP-302:
        ret.__name__ = spec.name
        ret.__file__ = self.path
        ret.__loader__ = self
        ret.__package__ = spec.name.rpartition(".")[0]
        return ret


sys.meta_path.append(BoaImporter())


def hash_input(compiler_input: CompilerInput) -> str:
    return compiler_input.sha256sum


# compute a fingerprint for a module which changes if any of its
# dependencies change
# TODO consider putting this in its own module
# TODO: refactor with new machinery in vyper 0.4.2+
def get_module_fingerprint(
    module_t: ModuleT, seen: dict["ImportInfo", str] = None
) -> str:
    seen = seen or {}
    fingerprints = []
    for stmt in module_t.import_stmts:
        import_info = stmt._metadata["import_info"]
        if id(import_info) not in seen:
            if isinstance(import_info.typ, ModuleT):
                fingerprint = get_module_fingerprint(import_info.typ, seen)
            else:
                fingerprint = hash_input(import_info.compiler_input)
            seen[id(import_info)] = fingerprint
        fingerprint = seen[id(import_info)]
        fingerprints.append(fingerprint)
    fingerprints.append(module_t._module.source_sha256sum)

    return sha256sum("".join(fingerprints))


def compiler_data(
    source_code: str,
    contract_name: str | None,
    filename: str | Path,
    deployer=None,
    **kwargs,
) -> CompilerData:
    global _disk_cache, _search_path

    path = Path(filename)
    resolved_path = Path(filename).resolve(strict=False)

    file_input = FileInput(
        contents=source_code, source_id=-1, path=path, resolved_path=resolved_path
    )

    search_paths = get_search_paths(_search_path)
    input_bundle = FilesystemInputBundle(search_paths)

    settings = Settings(**kwargs)
    ret = CompilerData(file_input, input_bundle, settings)
    if _disk_cache is None:
        return ret

    with anchor_settings(ret.settings):
        # note that this actually parses and analyzes all dependencies,
        # even if they haven't changed. an optimization would be to
        # somehow convince vyper (in ModuleAnalyzer) to get the module_t
        # from the cache.
        module_t = ret.annotated_vyper_module._metadata["type"]
    fingerprint = get_module_fingerprint(module_t)

    def get_compiler_data():
        with anchor_settings(ret.settings):
            # force compilation to happen so DiskCache will cache the compiled artifact:
            _ = ret.bytecode, ret.bytecode_runtime

            # workaround since CompilerData does not compute source_map
            if not hasattr(ret, "source_map"):
                # cache source map
                ret.source_map = _compute_source_map(ret)

        return ret

    assert isinstance(deployer, type) or deployer is None
    deployer_id = repr(deployer)  # a unique str identifying the deployer class
    cache_key = str((contract_name, filename, fingerprint, kwargs, deployer_id))

    ret = _disk_cache.caching_lookup(cache_key, get_compiler_data)

    if not hasattr(ret, "source_map"):
        # invalidate so it will be cached on the next run
        _disk_cache.invalidate(cache_key)

        # compute source map so it's available downstream
        ret.source_map = _compute_source_map(ret)

    return ret


def _compute_source_map(compiler_data: CompilerData) -> Any:
    _, source_map = compile_ir.assembly_to_evm(compiler_data.assembly_runtime)
    return source_map


def load(filename: str | Path, *args, **kwargs) -> _Contract:  # type: ignore
    name = Path(filename).stem
    # TODO: investigate if we can just put name in the signature
    if "name" in kwargs:
        name = kwargs.pop("name")
    with open(filename) as f:
        return loads(f.read(), *args, name=name, **kwargs, filename=filename)


def loads(
    source_code,
    *args,
    as_blueprint=False,
    name=None,
    filename=None,
    compiler_args=None,
    no_vvm=False,
    **kwargs,
):
    d = loads_partial(
        source_code, name, filename=filename, compiler_args=compiler_args, no_vvm=no_vvm
    )
    if as_blueprint:
        return d.deploy_as_blueprint(contract_name=name, **kwargs)
    else:
        return d.deploy(*args, contract_name=name, **kwargs)


def load_abi(filename: str, *args, name: str = None, **kwargs) -> ABIContractFactory:
    if name is None:
        name = Path(filename).stem
    # TODO: pass filename to ABIContractFactory
    with open(filename) as fp:
        return loads_abi(fp.read(), *args, name=name, **kwargs)


def loads_abi(json_str: str, *args, name: str = None, **kwargs) -> ABIContractFactory:
    return ABIContractFactory.from_abi_dict(json.loads(json_str), name, *args, **kwargs)


# load from .vyi file.
# NOTE: substantially same interface as load_abi and loads_abi, consider
# fusing them into load_interface?
def load_vyi(filename: str, name: str = None) -> ABIContractFactory:
    if name is None:
        name = Path(filename).stem
    with open(filename) as fp:
        return loads_vyi(fp.read(), name=name, filename=filename)


# load interface from .vyi file string contents.
# NOTE: since vyi files can be compiled in 0.4.1, this codepath can probably
# be refactored to use CompilerData (or straight loads_partial)
def loads_vyi(source_code: str, name: str = None, filename: str = None):
    global _search_path

    ast = parse_to_ast(source_code, is_interface=True)

    if name is None:
        name = "VyperContract.vyi"

    search_paths = get_search_paths(_search_path)
    input_bundle = FilesystemInputBundle(search_paths)

    # cf. CompilerData._resolve_imports
    if filename is not None:
        ctx = input_bundle.search_path(Path(filename).parent)
    else:
        ctx = contextlib.nullcontext()
    with ctx:
        _ = resolve_imports(ast, input_bundle)

    module_t = analyze_module(ast)
    abi = module_t.interface.to_toplevel_abi_dict()
    return ABIContractFactory(name, abi, filename=filename)


def loads_partial(
    source_code: str,
    name: str = None,
    filename: str | Path | None = None,
    dedent: bool = True,
    compiler_args: dict = None,
    no_vvm: bool = False,
) -> VyperDeployer:
    if filename is None:
        filename = "<unknown>"

    if dedent:
        source_code = textwrap.dedent(source_code)

    if not no_vvm:
        specifier_set = detect_version_specifier_set(source_code)
        # Use VVM only if the installed version is not in the specifier set
        if specifier_set is not None and not specifier_set.contains(vyper.__version__):
            version = _pick_vyper_version(specifier_set)
            filename = str(filename)  # help mypy
            return _loads_partial_vvm(source_code, version, name, filename)

    compiler_args = compiler_args or {}

    deployer_class = _get_default_deployer_class()
    data = compiler_data(source_code, name, filename, deployer_class, **compiler_args)
    return deployer_class(data, filename=filename)


def load_partial(filename: str, compiler_args=None):
    with open(filename) as f:
        return loads_partial(
            f.read(), name=filename, filename=filename, compiler_args=compiler_args
        )


def _loads_partial_vvm(
    source_code: str,
    version: Version,
    name: Optional[str],
    filename: str,
    base_path=None,
):
    global _disk_cache

    if base_path is None:
        base_path = Path(".")

    # install the requested version if not already installed
    vvm.install_vyper(version=version)

    def _compile():
        return vvm.compile_source(
            source_code, vyper_version=version, base_path=base_path
        )

    # separate _handle_output and _compile so that we don't trample
    # name and filename in the VVMDeployer from separate invocations
    # (with different values for name+filename).
    def _handle_output(compiled_src):
        compiler_output = compiled_src["<stdin>"]
        return VVMDeployer.from_compiler_output(
            compiler_output, name=name, filename=filename
        )

    # Ensure the cache is initialized
    if _disk_cache is None:
        return _handle_output(_compile())

    # Generate a unique cache key
    cache_key = f"{source_code}:{version}"

    # Check the cache and return the result if available
    ret = _disk_cache.caching_lookup(cache_key, _compile)

    # backwards compatibility: old versions of boa returned a VVMDeployer.
    # here we detect the case and invalidate the cache so it can recompile.
    if isinstance(ret, VVMDeployer):
        _disk_cache.invalidate(cache_key)
        ret = _disk_cache.caching_lookup(cache_key, _compile)

    return _handle_output(ret)


def from_etherscan(
    address: Any, name: str = None, uri: str = None, api_key: str = None
):
    addr = Address(address)

    if uri is not None or api_key is not None:
        etherscan = Etherscan(uri, api_key)
    else:
        etherscan = get_etherscan()

    abi = etherscan.fetch_abi(addr)
    return ABIContractFactory.from_abi_dict(abi, name=name).at(addr)


def _get_default_deployer_class():
    env = Env.get_singleton()
    if hasattr(env, "deployer_class"):
        return env.deployer_class
    return VyperDeployer


__all__ = []  # type: ignore
