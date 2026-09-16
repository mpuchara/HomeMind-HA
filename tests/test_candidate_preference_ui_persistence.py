from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CandidatePreferenceUiPersistenceTests(unittest.TestCase):
    def test_rebuilt_candidate_card_is_rehydrated_synchronously_from_cached_payload(self):
        text = (ROOT / "adaptive_ai/src/static/candidate_preference_ui.js").read_text(encoding="utf-8")
        self.assertIn("const latestByRef=new Map()", text)
        self.assertIn("function hydrateAddedNode(node)", text)
        self.assertIn("const cached=latestByRef.get(ref)", text)
        self.assertIn("if(cached)decorateCard(card,cached)", text)
        self.assertIn(".observe(root,{childList:true})", text)
        self.assertNotIn(".observe(root,{childList:true,subtree:true})", text)


if __name__ == "__main__":
    unittest.main()
