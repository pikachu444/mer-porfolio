import unittest

from portfolio_provenance import enrich_decision_provenance, prepare_post_signal_events
from portfolio_schema import parse_analysis_decision
from test_portfolio_schema import decision, insight


class PortfolioProvenanceTest(unittest.TestCase):
    def _posts(self):
        return [{
            "post_id": "123",
            "title": "알루미늄 공급",
            "url": "https://blog.naver.com/ranto28/123",
            "date": "2026-05-27",
            "signal_candidates": [{
                "exact_text": "Alcoa가 공급 제한의 수혜를 받을 수 있다.",
                "classification": "DIRECTIONAL_THESIS",
                "entity_name": "Alcoa",
                "entity_type": "company",
                "direction": "수혜",
                "horizon_kind": "cyclical",
                "catalysts": ["공급 제한"],
                "invalidation_conditions": ["공급 정상화"],
                "thesis_summary": "알루미늄 공급 제한 수혜",
            }],
        }]

    def test_prepares_content_addressed_source_event_and_rewrites_context_id(self):
        posts, events = prepare_post_signal_events(
            self._posts(),
            created_at="2026-06-01",
            model_id="gemini-3.1-flash-lite",
        )

        self.assertEqual(events[0]["signal_type"], "MER_THESIS")
        self.assertEqual(posts[0]["signal_candidates"][0]["signal_id"], events[0]["signal_id"])
        self.assertTrue(events[0]["signal_id"].startswith("sig_"))

    def test_same_entity_directional_thesis_becomes_mer_origin_with_ai_management(self):
        posts, events = prepare_post_signal_events(
            self._posts(),
            created_at="2026-06-01",
            model_id="gemini-3.1-flash-lite",
        )
        item = decision(decision_actor="AI")
        item["linked_signal_ids"] = [posts[0]["signal_candidates"][0]["signal_id"]]
        analysis = parse_analysis_decision({
            "analysis_date": "2026-06-01",
            "run_type": "regular",
            "insights": [insight()],
            "portfolio_decisions": [item],
            "watchlist": [],
        })

        enriched, all_events = enrich_decision_provenance(
            analysis,
            events,
            created_at="2026-06-01",
            model_id="gemini-3.5-flash",
        )

        result = enriched.portfolio_decisions[0]
        self.assertEqual(result["origin_signal_type"], "MER_THESIS")
        self.assertEqual(result["decision_actor"], "AI")
        self.assertEqual(len(all_events), 1)

    def test_signal_id_is_stable_across_model_and_processing_time(self):
        _, first = prepare_post_signal_events(
            self._posts(), created_at="2026-06-01", model_id="model-a"
        )
        _, second = prepare_post_signal_events(
            self._posts(), created_at="2026-06-10", model_id="model-b"
        )

        self.assertEqual(first[0]["signal_id"], second[0]["signal_id"])

    def test_event_records_the_model_that_summarized_each_cached_post(self):
        posts = self._posts()
        posts[0]["summary_model_id"] = "gemini-3.1-flash-lite"
        posts[0]["summary_model_version"] = "gemini-3.1-flash-lite-2026-05-07"

        _, events = prepare_post_signal_events(
            posts,
            created_at="2026-06-01",
            model_id="future-default-model",
        )

        self.assertEqual(
            events[0]["model_id"],
            "gemini-3.1-flash-lite-2026-05-07",
        )

    def test_bearish_mer_thesis_cannot_authorize_a_new_long(self):
        posts = self._posts()
        posts[0]["signal_candidates"][0]["direction"] = "피해"
        prepared, events = prepare_post_signal_events(
            posts,
            created_at="2026-06-01",
            model_id="gemini-3.1-flash-lite",
        )
        item = decision(decision_actor="AI")
        item["linked_signal_ids"] = [prepared[0]["signal_candidates"][0]["signal_id"]]
        analysis = parse_analysis_decision({
            "analysis_date": "2026-06-01",
            "run_type": "regular",
            "insights": [insight()],
            "portfolio_decisions": [item],
            "watchlist": [],
        })

        enriched, _ = enrich_decision_provenance(
            analysis,
            events,
            created_at="2026-06-01",
            model_id="gemini-3.5-flash",
        )

        self.assertEqual(
            enriched.portfolio_decisions[0]["provenance_status"],
            "legacy_unvalidated",
        )

    def test_mention_only_parent_cannot_create_investable_ai_etf_signal(self):
        posts = self._posts()
        posts[0]["signal_candidates"][0]["classification"] = "MENTION_ONLY"
        prepared, events = prepare_post_signal_events(
            posts,
            created_at="2026-06-01",
            model_id="gemini-3.1-flash-lite",
        )
        item = decision(
            name="알루미늄 ETF",
            code="ALUM",
            asset_type="etf",
            source_mentioned=False,
            source_scope="sector_only",
            basis="섹터 분석",
        )
        item["linked_signal_ids"] = [prepared[0]["signal_candidates"][0]["signal_id"]]
        analysis = parse_analysis_decision({
            "analysis_date": "2026-06-01",
            "run_type": "regular",
            "insights": [insight()],
            "portfolio_decisions": [item],
            "watchlist": [],
        })

        enriched, all_events = enrich_decision_provenance(
            analysis,
            events,
            created_at="2026-06-01",
            model_id="gemini-3.5-flash",
        )

        self.assertEqual(len(all_events), 1)
        self.assertEqual(
            enriched.portfolio_decisions[0]["provenance_status"],
            "legacy_unvalidated",
        )

    def test_explicit_signal_link_with_wrong_evidence_url_is_recorded_as_rejected(self):
        prepared, events = prepare_post_signal_events(
            self._posts(),
            created_at="2026-06-01",
            model_id="summary-model",
        )
        item = decision(decision_actor="AI")
        item["linked_signal_ids"] = [events[0]["signal_id"]]
        item["evidence_posts"][0]["url"] = "https://blog.naver.com/ranto28/other"
        analysis = parse_analysis_decision({
            "analysis_date": "2026-06-01",
            "run_type": "regular",
            "insights": [insight()],
            "portfolio_decisions": [item],
            "watchlist": [],
        })

        enriched, _ = enrich_decision_provenance(
            analysis,
            events,
            created_at="2026-06-01",
            model_id="decision-model",
        )

        result = enriched.portfolio_decisions[0]
        self.assertEqual(result["linked_signal_ids"], [])
        self.assertEqual(
            result["rejected_linked_signal_ids"],
            [prepared[0]["signal_candidates"][0]["signal_id"]],
        )

    def test_thesis_id_is_stable_across_summary_paraphrases(self):
        first_posts = self._posts()
        second_posts = self._posts()
        second_posts[0]["signal_candidates"][0]["thesis_summary"] = "같은 공급 부족 논지의 다른 표현"

        _, first_events = prepare_post_signal_events(
            first_posts, created_at="2026-06-01", model_id="model-a"
        )
        _, second_events = prepare_post_signal_events(
            second_posts, created_at="2026-06-02", model_id="model-b"
        )

        self.assertEqual(first_events[0]["thesis_id"], second_events[0]["thesis_id"])


if __name__ == "__main__":
    unittest.main()
