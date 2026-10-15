"""Нулевая релевантность не превращает алфавитные операции в рекомендации."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
import test_catalog_discovery as support


class NoMatchTests(unittest.TestCase):
    def test_unknown_goal_starts_with_vocabulary_search_without_arbitrary_cards(self):
        _, _, workflow = support.DiscoveryTests().workflow('http://127.0.0.1:18080')
        workflow.goal = 'Найди объект Созвездие'
        run = {'run_id':'r', 'limits':{'max_context_tokens':8192,'max_output_tokens':512}}
        context = workflow.initial_context(run, lambda payload: 100)
        self.assertEqual(run['selection_manifest']['selected_ids'], [])
        self.assertIn('API vocabulary', context)
        self.assertEqual(workflow.next_tool(run)['function']['name'], 'search_operations')

    def test_empty_search_requests_another_metadata_query_instead_of_inspection(self):
        _, _, workflow = support.DiscoveryTests().workflow('http://127.0.0.1:18080')
        run = {'run_id':'r', 'limits':{'max_context_tokens':8192,'max_output_tokens':512}}
        workflow.initial_context(run, lambda payload: 100)
        page = workflow.discovery('r', {'query':'nonexistent_literal_entity'})
        self.assertEqual(page['items'], [])
        self.assertIn('search_operations', page['next_hint'])
        self.assertNotIn('inspect_operation', page['next_hint'])
        run['steps'] = [{'action':{'name':'search_operations'},'observation':{'data':page}}]
        self.assertEqual(workflow.next_tool(run)['function']['name'], 'search_operations')


if __name__ == '__main__':
    unittest.main()
