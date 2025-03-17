import textwrap

import vyper.ast as vy_ast
import vyper.semantics.analysis as analysis
from vyper.ast.parse import parse_to_ast
from vyper.codegen.function_definitions import (
    generate_ir_for_external_function,
    generate_ir_for_internal_function,
)
from vyper.codegen.ir_node import IRnode
from vyper.codegen.module import _runtime_reachable_functions
from vyper.compiler.settings import anchor_settings
from vyper.exceptions import InvalidType
from vyper.ir import compile_ir, optimizer
from vyper.semantics.analysis.constant_folding import ConstantFolder
from vyper.semantics.analysis.utils import get_exact_type_from_node
from vyper.venom import generate_assembly_experimental, generate_ir

from boa.contracts.vyper.ir_executor import executor_from_ir

# id used internally for method id name
_METHOD_ID_VAR = "_calldata_method_id"


# visit dst_ast with the constants of src_ast. because of the way we
# construct the analysis, we don't insert most of the original contract
# into the vyper_function, relying on semantic data already being in the
# namespace. however, as of 0.4.0, constant folding doesn't rely on namespace,
# all the constants are handled by data structures internal to ConstantFolder.
# here we visit dst_ast with the constants of src_ast.
def _swipe_constants(src_ast, dst_ast):
    s = ConstantFolder(src_ast)
    s._get_constants()
    s.visit(dst_ast)


def compile_vyper_function(vyper_function, contract):
    """Compiles a vyper function and appends it to the top of the IR of a
    contract. This is useful for vyper `eval` and internal functions, where
    the runtime bytecode must be changed to add more runtime functionality
    (such as eval, and calling internal functions)
    (performance note: this function is very very slow!)
    """

    compiler_data = contract.compiler_data
    settings = compiler_data.settings

    with anchor_settings(settings):
        module_t = contract.module_t
        ast = parse_to_ast(vyper_function)

        # override namespace and add wrapper code at the top
        with contract.override_vyper_namespace():
            _swipe_constants(compiler_data.annotated_vyper_module, ast)
            analysis.analyze_module(ast)

        ast = ast.body[0]
        func_t = ast._metadata["func_type"]

        contract.ensure_id(func_t)
        funcinfo = generate_ir_for_external_function(ast, module_t)
        ir = funcinfo.common_ir

        entry_label = func_t._ir_info.external_function_base_entry_label

        ir = ["seq", ["goto", entry_label], ir]

        # use a dummy method id
        ir = ["with", _METHOD_ID_VAR, 0, ir]

        # first mush it with the rest of the IR in the contract to ensure
        # all labels are present, and then optimize all together
        # (use unoptimized IR, ir_executor can't handle optimized selector tables)
        _, contract_runtime = contract.unoptimized_ir
        ir_list = ["seq", ir, contract_runtime]
        reachable = func_t.reachable_internal_functions

        already_compiled = _runtime_reachable_functions(module_t, contract)
        missing_functions = reachable.difference(already_compiled)
        # TODO: cache function compilations or something
        for f in missing_functions:
            assert f.ast_def is not None
            contract.ensure_id(f)
            ir_list.append(
                generate_ir_for_internal_function(f.ast_def, module_t, False).func_ir
            )

        ir = IRnode.from_list(ir_list)
        if settings.experimental_codegen:
            venom_code = generate_ir(ir, settings)
            assembly = generate_assembly_experimental(
                venom_code, optimize=settings.optimize
            )

        else:
            ir = optimizer.optimize(ir)
            assembly = compile_ir.compile_to_assembly(ir)

        bytecode, source_map = compile_ir.assembly_to_evm(assembly)
        bytecode += contract.data_section
        typ = func_t.return_type

        # generate the IR executor
        if settings.experimental_codegen:
            ir_executor = None
        else:
            ir_executor = executor_from_ir(ir, compiler_data)

        return ast, ir_executor, bytecode, source_map, typ


def generate_bytecode_for_internal_fn(fn):
    """Wraps internal fns with an external fn and generated bytecode"""
    contract = fn.contract
    fn_name = fn.func_t.name
    fn_ast = fn.func_t.ast_def

    fn_args = ", ".join([arg.name for arg in fn.func_t.arguments])

    return_sig = ""
    fn_call = ""
    if fn.func_t.return_type:
        return_sig = f" -> {fn_ast.returns.node_source_code}"
        fn_call = "return "
    fn_call += f"self.{fn_name}({fn_args})"

    # construct the signature of the external function
    # little alignment of args with defaults
    n_kwargs = len(fn_ast.args.defaults)
    n_posargs = len(fn_ast.args.args) - n_kwargs
    sig_args = []
    for i, arg in enumerate(fn_ast.args.args):
        if i < n_posargs:
            sig_args.append(arg.node_source_code)
        else:
            default = fn_ast.args.defaults[i - n_posargs].node_source_code
            sig_args.append(f"{arg.node_source_code} = {default}")

    fn_sig = f"def __boa_private_{fn_name}__(" + ", ".join(sig_args) + ")"
    fn_sig += f"{return_sig}:"

    wrapper_code = f"""
@external
@payable
{fn_sig}
    {fn_call}
    """
    return compile_vyper_function(wrapper_code, contract)


def generate_bytecode_for_arbitrary_stmt(source_code, contract):
    """Wraps arbitrary stmts with external fn and generates bytecode"""

    ast = parse_to_ast(source_code)

    ast = ast.body[0]

    return_sig = ""
    debug_body = source_code

    if isinstance(ast, vy_ast.Expr):
        with contract.override_vyper_namespace():
            try:
                ast_typ = get_exact_type_from_node(ast.value)
                return_sig = f"-> {ast_typ}"
                debug_body = f"return {source_code}"
            except InvalidType:
                pass

    # wrap code in function so that we can easily generate code for it
    wrapper_code = textwrap.dedent(
        f"""
        @external
        @payable
        def __boa_debug__() {return_sig}:
            {debug_body}
    """
    )
    return compile_vyper_function(wrapper_code, contract)
