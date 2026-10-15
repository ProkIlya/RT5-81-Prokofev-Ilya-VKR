"""Индекс предоставляет лексику произвольной спецификации, не словарь стенда."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from api_agent.agent.discovery import CatalogDiscovery
from api_agent.openapi import parse_document
from api_agent.policy import ProjectConfig


class SearchTermsTests(unittest.TestCase):
    def discovery(self, allowed=None):
        names = {"/api/trips": "post_api_trips", "/api/scenarios": "get_api_scenarios", "/api/users/profile": "get_api_users_profile", "/api/users/login": "post_api_users_login"}
        catalog = parse_document({"openapi":"3.0.3", "info":{"title":"terms","version":"1"}, "paths":{
            path:{"post" if name.startswith("post") else "get":{"operationId":name,"responses":{"200":{"description":"ok"}}}}
            for path,name in names.items()}})
        ids = list(names.values()) if allowed is None else allowed
        config = ProjectConfig.create("p","http://127.0.0.1:18080",catalog,mode="TEST",resettable=True,operations={n:("C" if n.startswith("post") else "A") for n in ids})
        return CatalogDiscovery(catalog,ids,config)

    def test_underscore_operation_ids_match_individual_words(self):
        found = self.discovery().ranked("users profile")
        self.assertEqual(found[0][0], "get_api_users_profile")

    def test_index_exposes_all_allowed_resource_groups_for_model_translation(self):
        discovery=self.discovery()
        self.assertEqual({g['query'] for g in discovery.index(discovery.allowed)['groups']}, {'trips','scenarios','users'})

    def test_index_is_derived_from_an_unrelated_specification(self):
        catalog=parse_document({'openapi':'3.0.3','info':{'title':'unrelated','version':'1'},'paths':{
            '/v9/celestial/{key}':{'get':{'operationId':'fetchNebula','summary':'Read a celestial object','parameters':[{'name':'key','in':'path','required':True,'schema':{'type':'string'}}],'responses':{'200':{'description':'ok'}}}},
            '/v9/manuscripts':{'get':{'operationId':'browseArchive','summary':'Browse published texts','responses':{'200':{'description':'ok'}}}}}})
        config=ProjectConfig.create('p','http://127.0.0.1:18080',catalog,mode='READ_ONLY',resettable=True,operations={n:'A' for n in catalog.operations})
        discovery=CatalogDiscovery(catalog,list(catalog.operations),config)
        index=discovery.index(discovery.allowed)
        self.assertEqual({g['query'] for g in index['groups']},{'celestial','manuscripts'})
        self.assertNotIn('trip',str(index))
        self.assertEqual(discovery.ranked('fetch nebula')[0][0],'fetchNebula')

    def test_aliases_do_not_expand_permissions(self):
        discovery = self.discovery(["get_api_scenarios"])
        self.assertEqual(discovery.ranked("заявка"), [])
        self.assertEqual([n for n,_ in discovery.ranked("")], ["get_api_scenarios"])
        self.assertEqual([g['query'] for g in discovery.index(discovery.allowed)['groups']],['scenarios'])


if __name__ == "__main__":
    unittest.main()
