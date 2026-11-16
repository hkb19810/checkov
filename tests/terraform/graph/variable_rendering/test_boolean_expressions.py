import itertools
import pytest
from checkov.terraform.graph_builder.variable_rendering.evaluate_terraform import evaluate_terraform
from checkov.terraform.graph_builder.utils import get_referenced_vertices_in_value
from checkov.common.util.var_utils import is_terraform_variable_dependent

CASES = [
    ('!false', True), ('!true', False), ('!!false', False), ('!!true', True),
    ('!(false || false)', True), ('!(true && false)', True),
    ('true || false && false', True), ('(true || false) && false', False),
    ('(False || False) ? 1 : 0', 0), ('(false || false) ? 1 : 0', 0),
    ('false || false || false', False), ('false || true || false', True),
    ('true ? (false ? 1 : 2) : 3', 2), ('false ? 1 : true ? 2 : 3', 2),
    ('true != false', True), ('false != false', False),
    ('"keep!text"', 'keep!text'), ('"some || text"', 'some || text'),
    ('!jsondecode("false")', True), ('!alltrue([true, false])', True),
]
for a, b in itertools.product((False, True), repeat=2):
    for spelling in (str, lambda x: str(x).lower()):
        x, y = spelling(a), spelling(b)
        CASES.extend([(f'{x} || {y}', a or b), (f'{x} && {y}', a and b),
                      (f'({x} || {y}) ? 1 : 0', int(a or b)),
                      (f'!({x} && {y})', not(a and b))])

@pytest.mark.parametrize('expression,expected', CASES)
def test_known_expression(expression, expected):
    actual = evaluate_terraform(expression)
    assert type(actual) is type(expected) and actual == expected

@pytest.mark.parametrize('expression', [
    '!var.flag', 'false || var.flag', 'true && var.flag',
    '(var.a || var.b) ? 1 : 0', '!1', '1 || false',
])
def test_unresolved_or_invalid_is_not_assumed(expression):
    assert evaluate_terraform(expression) == expression

@pytest.mark.parametrize('expression,expected', [
    ('!var.flag', ['var.flag']),
    ('(var.a || var.b) ? 1 : 0', ['var.a', 'var.b']),
    ('var.a||var.b', ['var.a', 'var.b']),
    ('!(local.a && var.b)', ['local.a', 'var.b']),
    ('"literal !var.flag"', []),
])
def test_reference_detection(expression, expected):
    result = get_referenced_vertices_in_value(expression, {}, [])
    assert [r.origin_value for r in result] == expected

@pytest.mark.parametrize('expression', ['!var.flag', '(var.flag)', 'false || var.flag'])
def test_unknown_detection(expression):
    assert is_terraform_variable_dependent(expression)

def test_excessive_negation_remains_unresolved():
    expression = '!' * 1000 + 'false'
    assert evaluate_terraform(expression) == expression


@pytest.mark.parametrize('value,expected_handled,expected', [
    (['false || var.flag'], True, ['false || var.flag']),
    (['${false || var.flag}'], True, ['${false || var.flag}']),
    (['false || false'], True, [False]),
    ({'flags': ['!false'], 'version': '1.19'}, True, {'flags': [True], 'version': '1.19'}),
    (['123', 'false', '1.19', '${var.name}'], False, ['123', 'false', '1.19', '${var.name}']),
    ({'groups': {'first': 'eastus', 'second': 'westus2'}}, False,
     {'groups': {'first': 'eastus', 'second': 'westus2'}}),
])
def test_boolean_containers_preserve_literal_types(value, expected_handled, expected):
    from checkov.terraform.graph_builder.variable_rendering.boolean_expressions import evaluate_boolean_container
    handled, result = evaluate_boolean_container(value, evaluate_terraform)
    assert handled is expected_handled
    assert result == expected


def test_substitution_preserves_negation_precedence():
    from checkov.terraform.graph_builder.variable_rendering.evaluate_terraform import replace_string_value
    expression = replace_string_value('!var.flag', 'var.flag', 'false || var.other')
    assert expression == '!(false || var.other)'
    assert evaluate_terraform(expression) == expression
    assert evaluate_terraform(replace_string_value(expression, 'var.other', True)) is False
    assert evaluate_terraform(replace_string_value(expression, 'var.other', False)) is True
