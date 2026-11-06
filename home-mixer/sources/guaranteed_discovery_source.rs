use crate::models::candidate::PostCandidate;
use crate::models::query::ScoredPostsQuery;
use crate::util::guaranteed_discovery::{
    BLOCKS_CHECKED, DiscoveryClient, FOLLOWS_CHECKED, MUTES_CHECKED,
};
use std::sync::Arc;
use std::sync::atomic::Ordering;
use tonic::async_trait;
use xai_candidate_pipeline::source::Source;

pub struct GuaranteedDiscoverySource {
    pub(crate) client: Arc<DiscoveryClient>,
}

#[async_trait]
impl Source<ScoredPostsQuery, PostCandidate> for GuaranteedDiscoverySource {
    fn enable(&self, query: &ScoredPostsQuery) -> bool {
        query.discovery_slate.as_ref().is_some_and(|slate| {
            slate.graph_checks.load(Ordering::Relaxed)
                == (BLOCKS_CHECKED | MUTES_CHECKED | FOLLOWS_CHECKED)
        })
    }

    async fn source(&self, query: &ScoredPostsQuery) -> Result<Vec<PostCandidate>, String> {
        let Some(slate) = &query.discovery_slate else {
            return Ok(vec![]);
        };
        let plans = self.client.candidates(query.user_id).await?;
        let plans: Vec<_> = plans
            .into_iter()
            .filter(|plan| {
                let author = plan.author_id as i64;
                plan.author_id != query.user_id
                    && !query.user_features.followed_user_ids.contains(&author)
                    && !query.user_features.blocked_user_ids.contains(&author)
                    && !query.user_features.muted_user_ids.contains(&author)
                    && !query.seen_ids.contains(&plan.post_id)
                    && !query.served_ids.contains(&plan.post_id)
            })
            .collect();
        let _ = slate.plans.set(
            plans.iter().copied().enumerate()
                .map(|(priority, plan)| (plan.post_id, (priority, plan)))
                .collect(),
        );
        Ok(plans.into_iter().map(|plan| PostCandidate {
            tweet_id: plan.post_id,
            author_id: plan.author_id,
            discovery_only: true,
            discovery_unverified: true,
            ..Default::default()
        }).collect())
    }
}
