use crate::models::candidate::PostCandidate;
use crate::models::query::ScoredPostsQuery;
use crate::params::RESULT_SIZE;
use crate::util::guaranteed_discovery::{OfferPost, SLOTS_PER_DISCOVERY};
use std::collections::HashSet;
use xai_candidate_pipeline::filter::{Filter, FilterResult};
use xai_visibility_filtering::models::Action;

pub struct GuaranteedDiscoverySlots;

impl Filter<ScoredPostsQuery, PostCandidate> for GuaranteedDiscoverySlots {
    fn enable(&self, query: &ScoredPostsQuery) -> bool {
        query.discovery_slate.as_ref().and_then(|slate| slate.plans.get()).is_some()
    }

    fn filter(
        &self,
        query: &ScoredPostsQuery,
        candidates: Vec<PostCandidate>,
    ) -> FilterResult<PostCandidate> {
        let Some(slate) = &query.discovery_slate else {
            return FilterResult { kept: candidates, removed: vec![] };
        };
        let Some(plans) = slate.plans.get() else {
            return FilterResult { kept: candidates, removed: vec![] };
        };
        let organic_count = candidates.iter().filter(|post| {
            !post.discovery_only && !post.video_carousel_extra
        }).count();
        let mut eligible: Vec<_> = candidates.iter().filter_map(|post| {
            let &(priority, plan) = plans.get(&post.tweet_id)?;
            (!post.discovery_unverified
                && post.author_id == plan.author_id
                && post.in_network == Some(false)
                && post.author_blocks_viewer == Some(false)
                && post.retweeted_tweet_id.is_none()
                && post.in_reply_to_tweet_id.is_none()
                && !post.video_carousel_extra
                && matches!(post.visibility_action.as_ref(), Some(Action::Allow)))
                .then_some((priority, plan, post.discovery_only))
        }).collect();
        eligible.sort_by_key(|(priority, _, _)| *priority);

        // Use actual hydrated inventory to avoid reserving a slot on tiny slates.
        let mut budget = RESULT_SIZE.min(organic_count + eligible.len()) / SLOTS_PER_DISCOVERY;
        while budget > 0 {
            let added = eligible.iter().take(budget).filter(|(_, _, only)| *only).count();
            if RESULT_SIZE.min(organic_count + added) / SLOTS_PER_DISCOVERY >= budget {
                break;
            }
            budget -= 1;
        }
        let reserved: Vec<_> = eligible.into_iter().take(budget).map(|(_, plan, _)| {
            OfferPost { post_id: plan.post_id, stage: plan.stage }
        }).collect();
        let ids: HashSet<_> = reserved.iter().map(|post| post.post_id).collect();

        let mut kept = Vec::new();
        let mut removed = Vec::new();
        let mut discovery = std::collections::HashMap::new();
        for candidate in candidates {
            if ids.contains(&candidate.tweet_id) {
                discovery.insert(candidate.tweet_id, candidate);
            } else if candidate.discovery_only {
                removed.push(candidate);
            } else {
                kept.push(candidate);
            }
        }
        for (slot, post) in reserved.iter().enumerate() {
            if let Some(candidate) = discovery.remove(&post.post_id) {
                // Space the reserved posts throughout the page before ad blending.
                let index = (2 + slot * SLOTS_PER_DISCOVERY).min(kept.len());
                kept.insert(index, candidate);
            }
        }
        let _ = slate.selected.set(reserved);
        FilterResult { kept, removed }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::util::guaranteed_discovery::{DiscoverySlate, Plan};
    use std::sync::Arc;

    #[test]
    fn reserves_by_queue_priority_after_visibility_and_fills_from_organic() {
        let slate = Arc::new(DiscoverySlate::default());
        let _ = slate.plans.set(
            [101, 102, 103, 104]
                .into_iter()
                .enumerate()
                .map(|(rank, post_id)| (post_id, (rank, Plan { post_id, author_id: post_id, stage: 0 })))
                .collect(),
        );
        let query = ScoredPostsQuery { discovery_slate: Some(slate.clone()), ..Default::default() };
        let mut candidates: Vec<_> = (1..=35).map(|tweet_id| PostCandidate {
            tweet_id,
            ..Default::default()
        }).collect();
        candidates.extend([101, 102, 103, 104].into_iter().rev().map(|tweet_id| PostCandidate {
            tweet_id,
            author_id: tweet_id,
            discovery_only: true,
            discovery_unverified: tweet_id == 102,
            in_network: Some(false),
            author_blocks_viewer: Some(false),
            visibility_action: (tweet_id != 101).then_some(Action::Allow),
            ..Default::default()
        }));
        let result = GuaranteedDiscoverySlots.filter(&query, candidates);
        let reserved: Vec<_> = slate.selected.get().unwrap().iter().map(|p| p.post_id).collect();
        assert_eq!(reserved, [103, 104]);
        assert_eq!(result.kept[2].tweet_id, 103);
        assert_eq!(result.kept[12].tweet_id, 104);
        assert_eq!(result.removed.len(), 2);
    }
}
