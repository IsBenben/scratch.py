from dataclasses import dataclass
from error import Error, raise_error
from nodes import Block, NodeVisitor, FunctionDeclaration
from records import Record
from typing import Optional
from utils import generate_id
from values import *
import json

def json_with_settings(dump_fn, *args, **kwargs):
    return dump_fn(*args, **kwargs, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

PRO = 'procedure_'

@dataclass
class BlockType:
    inputs: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    required: bool = True

BLOCK_TYPES: dict[str, BlockType] = {
    'looks_say': BlockType(inputs=('MESSAGE',)),
    'looks_sayforsecs': BlockType(inputs=('MESSAGE', 'SECS')),
    'operator_add': BlockType(inputs=('NUM1', 'NUM2')),
    'operator_subtract': BlockType(inputs=('NUM1', 'NUM2')),
    'operator_multiply': BlockType(inputs=('NUM1', 'NUM2')),
    'operator_divide': BlockType(inputs=('NUM1', 'NUM2')),
    'operator_mod': BlockType(inputs=('NUM1', 'NUM2')),
    'data_setvariableto': BlockType(inputs=('VALUE',), fields=('VARIABLE',)),
    'operator_and': BlockType(inputs=('OPERAND1', 'OPERAND2')),
    'operator_or': BlockType(inputs=('OPERAND1', 'OPERAND2')),
    'operator_not': BlockType(inputs=('OPERAND',), required=False),
    'control_if': BlockType(inputs=('CONDITION', 'SUBSTACK')),
    'control_if_else': BlockType(inputs=('CONDITION', 'SUBSTACK', 'SUBSTACK2')),
    'operator_gt': BlockType(inputs=('OPERAND1', 'OPERAND2')),
    'operator_lt': BlockType(inputs=('OPERAND1', 'OPERAND2')),
    'operator_equals': BlockType(inputs=('OPERAND1', 'OPERAND2')),
    'control_repeat_until': BlockType(inputs=('CONDITION', 'SUBSTACK')),
    'control_wait': BlockType(inputs=('DURATION',))
}

class Interpreter(NodeVisitor):
    def __init__(self) -> None:
        self.record = Record()
        self.project: dict = json.load(open('./src/model.json', encoding='utf-8'))
        self.blocks: dict[str, dict] = self.project['targets'][1]['blocks']
        self.variables: dict[str, list[str]] = self.project['targets'][0]['variables']
        self.parent_function: Optional[FunctionDeclaration] = None
    
    def visit_Block(self, node) -> BlockList | None:
        if not node.body:
            return None
        start_id = end_id = None
        event = None

        # Run in new record
        old_record = self.record
        self.record = Record(self.record)
        # If the parent function is not None, add the arguments to the record
        if self.parent_function is not None:
            function_node = self.parent_function
            function_id = generate_id((f'{PRO}name', self.record.resolve_function(function_node.name), function_node.name))
            arg_ids = [generate_id((f'{PRO}argument', function_id, arg_name)) for arg_name in function_node.args]
            for arg_name, arg_id in zip(function_node.args, arg_ids):
                self.record.declare_variable('argument', arg_name, arg_id)
            self.parent_function = None

        for statement in node.body:
            block = self.visit(statement)
            if isinstance(block, BlockList):
                # Simple understand: doubly linked lists
                statement_start, statement_end = block.get_start_end()
                if start_id is None:
                    start_id = statement_start
                if event is not None:
                    self.blocks[statement_start]['parent'] = end_id
                    event['next'] = statement_start
                end_id = statement_end
                event = self.blocks[end_id]
            elif block is not None:
                raise_error(Error('Interpret', 'Invalid statement'))
        
        self.record = old_record

        if start_id is None:
            return None
        return BlockList((start_id, end_id))

    def visit_Identifier(self, node) -> Variable | Block:
        variable_record = self.record.resolve_variable(node.name)
        variable = variable_record.variables[node.name]
        if variable.type == 'variable':
            return Variable(node.name, variable_record)
        else:  # argument
            # In scratch, it's a function call
            call_id = generate_id(('call', node))
            self.blocks[call_id] = {
                "opcode": "argument_reporter_string_number",
                "next": None,
                # Only return the value, and let external code to set parent
                "parent": None,
                "inputs": {},
                # more=arg_id, because it's only an argument, and not a variable
                "fields": { "VALUE": [variable.more, None] },
                "shadow": False,
                "topLevel": False
            }
            return Block(call_id)
    
    def visit_VariableDeclaration(self, node) -> None:
        self.record.declare_variable('variable', node.name, node.is_const)
        variable_id = generate_id(('variable', node.name, self.record))
        self.variables[variable_id] = [variable_id, '[NOT ASSIGNED]']
    
    def visit_String(self, node) -> String:
        return String(node.value)

    def visit_Program(self, node) -> Block:
        event_id = generate_id(('event', node))
        event = self.blocks[event_id] = {
            'opcode': 'event_whenflagclicked',
            'next': None,
            'parent': None,
            'inputs': {},
            'fields': {},
            'shadow': False,
            'topLevel': True,
        }
        for statement in node.body:
            block = self.visit(statement)
            if isinstance(block, BlockList):
                # Simple understand: doubly linked lists
                statement_start, statement_end = block.get_start_end()
                self.blocks[statement_start]['parent'] = event_id
                event['next'] = statement_start
                event_id = statement_end
                event = self.blocks[event_id]
            elif block is not None:
                raise_error(Error('Interpret', 'Invalid statement'))
        return Block(event_id)

    def visit_Number(self, node) -> Integer:
        return Integer(node.value)

    def visit_FunctionCall(self, node) -> Block:
        if self.record.has_function(node.name):
            return self._visit_custom_FunctionCall(node)
        return self._visit_builtin_FunctionCall(node)

    def _visit_custom_FunctionCall(self, node) -> Block:
        # Custom functions
        call_id = generate_id(('call', node))
        function_node = self.record.resolve_function(node.name).functions[node.name]
        function_id = generate_id((f'{PRO}name', self.record, function_node.name))
        arg_ids = [generate_id((f'{PRO}argument', function_id, arg_name)) for arg_name in function_node.args]

        # mutation
        mutation = {
            'tagName': 'mutation',
            'children': [],
            'proccode': f'{function_id}{" %s" * len(function_node.args)}',
            'argumentids': json_with_settings(json.dumps, arg_ids),
            'warp': 'false'
        }

        if len(node.args) < len(function_node.args):
            raise_error(Error('Interpret', f'Too few arguments in function {function_node.name}'))

        inputs = {}
        for i in range(len(node.args)):
            arg = self.visit(node.args[i])
            if i < len(function_node.args):
                # See _visit_builtin_FunctionCall function
                if isinstance(arg, Block):
                    self.blocks[arg.get_start_end()[0]]['parent'] = call_id
                arg_name = arg_ids[i]
                arg_value = arg.get_value()
                inputs[arg_name] = arg_value
            else:
                raise_error(Error('Interpret', f'Too many arguments in function {node.name}'))
        self.blocks[call_id] = {
            "opcode": "procedures_call",
            "next": None,
            "parent": None,
            "inputs": inputs,
            "fields": {},
            "shadow": False,
            "topLevel": False,
            "mutation": mutation
        }
        return Block(call_id)

    def _visit_builtin_FunctionCall(self, node) -> Block:
        # Built-in functions
        call_id = generate_id(('call', node))
        if node.name not in BLOCK_TYPES:
            # if cannot find the function in built-in functions, raise an error
            raise_error(Error('Interpret', f'Function {node.name} not declared'))
        bt = BLOCK_TYPES[node.name]  # block type
        if len(node.args) < len(bt.fields + bt.inputs) and bt.required:
            raise_error(Error('Interpret', f'Too few arguments in function {node.name}'))
        fields, inputs = {}, {}
        for i in range(len(node.args)):
            arg = self.visit(node.args[i])
            if i < len(bt.fields):
                if isinstance(arg, Variable):
                    # Then, by default we think it set the variables
                    # TODO: modify the behavior
                    variable = arg.value[1].variables[arg.value[0]]
                    if variable.type == 'argument':
                        raise_error(Error('Interpret', 'Cannot set an argument variable'))
                    # more=is_const in there, because it's only a variable, and not an argument
                    if variable.more:
                        variable.change_counts += 1
                        # First change: init constant value
                        # Second change: raise an error
                        if variable.change_counts >= 2:
                            raise_error(Error('Interpret', 'Cannot set a constant variable'))
                fields[bt.fields[i]] = arg.get_id_name()
            elif i < len(bt.fields + bt.inputs):
                # Set a block parent
                if isinstance(arg, Block):
                    self.blocks[arg.get_start_end()[0]]['parent'] = call_id

                arg_name = bt.inputs[i - len(bt.fields)]
                if 'STACK' in arg_name:
                    arg_value = arg.get_stack()
                else:
                    arg_value = arg.get_value()
                inputs[arg_name] = arg_value
            else:
                raise_error(Error('Interpret', f'Too many arguments in function {node.name}'))
        self.blocks[call_id] = {
            'opcode': node.name,
            'next': None,
            'parent': None,
            'inputs': inputs,
            'fields': fields,
            'shadow': False,
            'topLevel': False
        }
        return Block(call_id)
    
    def visit_FunctionDeclaration(self, node) -> None:
        # ids
        definition_id = generate_id((f'{PRO}definition', node))
        prototype_id = generate_id((f'{PRO}prototype', node))
        self.record.declare_function(node)
        function_id = generate_id((f'{PRO}name', self.record, node.name))
        arg_ids = [generate_id((f'{PRO}argument', function_id, arg_name)) for arg_name in node.args]

        # mutation
        mutation = {
            'tagName': 'mutation',
            'children': [],
            'proccode': f'{function_id}{" %s" * len(node.args)}',
            'argumentids': json_with_settings(json.dumps, arg_ids),
            'argumentnames':  json_with_settings(json.dumps, arg_ids),
            'argumentdefaults': json_with_settings(json.dumps, ['' * len(node.args)]),
            'warp': 'false'
        }

        # inputs
        inputs = {}
        for arg_id in arg_ids:
            self.blocks[arg_id] = {
                "opcode": "argument_reporter_string_number",
                "next": None,
                "parent": prototype_id,
                "inputs": {},
                "fields": { "VALUE": [arg_id, None] },
                "shadow": True,
                "topLevel": False
            }
            inputs[arg_id] = [1, arg_id]

        # go to inner
        self.parent_function = node
        inner_id = self.visit(node.body).get_start_end()[0]
        self.blocks[inner_id]['parent'] = definition_id

        # blocks
        self.blocks[definition_id] = {
            'opcode': 'procedures_definition',
            'next': inner_id,
            'parent': None,
            'inputs': { 'custom_block': [1, prototype_id] },
            'fields': {},
            'shadow': False,
            'topLevel': True,
        }
        self.blocks[prototype_id] = {
            'opcode': 'procedures_prototype',
            "next": None,
            "parent": definition_id,
            "inputs": inputs,
            "fields": {},
            "shadow": True,
            "topLevel": False,
            "mutation": mutation
        }
