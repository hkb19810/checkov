import copy
import unittest

from checkov.terraform.checks.resource import resource_existence as h


class TestExistence(unittest.TestCase):
    def test_multiplicity(self):
        cases = [({}, True), ({'count':[0]}, False), ({'count':[1]}, True), ({'count':[2]}, True),
                 ({'count':['0']}, False), ({'count':['1']}, True), ({'count':[0.0]}, False),
                 ({'count':[0.5]}, None), ({'count':[-1]}, None), ({'count':[False]}, None),
                 ({'count':[True]}, None), ({'count':[None]}, None), ({'count':[]}, None),
                 ({'count':[0,1]}, None), ({'count':['${var.enabled ? 1 : 0}']}, None),
                 ({'count':[0], 'for_each':[{}]}, None), ({'for_each':[{}]}, False),
                 ({'for_each':[{'one':'${var.unknown}'}]}, True), ({'for_each':[['one']]}, None),
                 ({'for_each':[[]]}, None), ({'for_each':[]}, None), ({'for_each':[None]}, None),
                 ({'for_each':['${toset([])}']}, False), ({'for_each':['${toset(["one"])}']}, True),
                 ({'for_each':['${var.unknown}']}, None), ({'for_each':[{'${var.key}':1}]}, None),
                 ({'for_each':[{'var.literal':1}]}, True), ({'for_each':[set()]}, False),
                 ({'for_each':[{'one'}]}, True), ({'for_each':[{h.TRUE_AFTER_UNKNOWN}]}, None)]
        for conf, expected in cases:
            with self.subTest(conf=conf):
                original = copy.deepcopy(conf)
                self.assertIs(h.multiplicity(conf), expected)
                self.assertEqual(conf, original)
    def test_module_ancestors(self):
        module = {h.A.BLOCK_TYPE:'module', h.A.BLOCK_NAME:'child', h.A.FILE_PATH:'C:/root/main.tf', 'count':0}
        child = {h.A.BLOCK_TYPE:'resource', h.A.SOURCE_MODULE_OBJECT:{'path':'C:\\root\\main.tf','name':'child'}}
        self.assertIs(h.node_existence(child, {1:module, 2:child}), False)
        module['count'] = 1
        self.assertIs(h.node_existence(child, {1:module, 2:child}), True)
        module['count'] = '${var.unknown}'
        self.assertIsNone(h.node_existence(child, {1:module, 2:child}))
        self.assertIsNone(h.node_existence(child, {2:child}))
        child['count'] = 0
        self.assertIs(h.node_existence(child, {2:child}), False)
    def test_unknown_values_do_not_make_known_keys_unknown(self):
        self.assertTrue(h.multiplicity({'for_each': {'one': object()}}, wrapped=False))
    def test_bad_scope(self):
        self.assertIsNone(h.node_existence({h.A.SOURCE_MODULE_OBJECT:'broken'}, {}))
    def test_no_global_behavior_change(self):
        from checkov.terraform.checks.resource.base_resource_check import BaseResourceCheck
        from checkov.common.models.enums import CheckCategories, CheckResult
        class Probe(BaseResourceCheck):
            def __init__(self):
                super().__init__('Existence test', 'TEST_EXISTENCE', [CheckCategories.GENERAL_SECURITY], ['aws_ebs_volume'])
            def scan_resource_conf(self, conf):
                return CheckResult.FAILED
        check = Probe()
        self.assertEqual(check.scan_entity_conf({'count':[0]}, 'aws_ebs_volume'), CheckResult.UNKNOWN)
        self.assertEqual(check.scan_entity_conf({'count':[1]}, 'aws_ebs_volume'), CheckResult.FAILED)

    def test_unbound_map_is_not_proof_of_zero_instances(self):
        variable = {h.A.BLOCK_TYPE: 'variable', h.A.BLOCK_NAME: 'items', h.A.FILE_PATH: 'main.tf'}
        node = {h.A.BLOCK_TYPE: 'resource', h.A.RESOURCE_TYPE: 'aws_instance',
                h.A.TF_RESOURCE_ADDRESS: 'aws_instance.main', 'for_each': {},
                h.A.RENDERING_BREADCRUMBS: {'for_each': [
                    {'type': 'variable', 'name': 'items', 'path': 'main.tf'}]}}
        nodes = {1: node, 2: variable}
        class Graph:
            def nodes(self, data=False):
                return list(nodes.items())
        conf = {h.A.TF_RESOURCE_ADDRESS: 'aws_instance.main', 'for_each': [{}]}
        self.assertIsNone(h.resource_existence(conf, Graph(), 'aws_instance'))
        self.assertIsNone(h.node_existence(node, nodes))
        variable['default'] = {}
        self.assertIs(h.resource_existence(conf, Graph(), 'aws_instance'), False)
        del variable['default']
        node['for_each'] = {'known_key': None}
        conf['for_each'] = [{'known_key': None}]
        self.assertIs(h.resource_existence(conf, Graph(), 'aws_instance'), True)

if __name__ == '__main__':
    unittest.main(verbosity=2)
