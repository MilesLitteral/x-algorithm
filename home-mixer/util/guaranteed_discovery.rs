use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fmt;
use std::sync::OnceLock;
use std::time::Duration;

pub(crate) const SLOTS_PER_DISCOVERY: usize = 10;

pub(crate) struct DiscoveryClient {
    http: reqwest::Client,
    base: String,
    token: String,
}

impl fmt::Debug for DiscoveryClient {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("DiscoveryClient").finish_non_exhaustive()
    }
}

impl DiscoveryClient {
    pub fn from_env() -> Option<Self> {
        let port = match std::env::var("GUARANTEED_DISCOVERY_PORT") {
            Ok(port) => match port.parse::<u16>() {
                Ok(port) if port != 0 => port,
                _ => {
                    tracing::warn!("Invalid GUARANTEED_DISCOVERY_PORT");
                    return None;
                }
            },
            Err(_) => return None,
        };
        let token = match std::env::var("GUARANTEED_DISCOVERY_TOKEN") {
            Ok(token) if !token.is_empty() => token,
            _ => {
                tracing::warn!("Missing GUARANTEED_DISCOVERY_TOKEN");
                return None;
            }
        };
        match reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_millis(150))
            .build()
        {
            Ok(http) => Some(Self {
                http,
                base: format!("http://127.0.0.1:{port}"),
                token,
            }),
            Err(error) => {
                tracing::warn!(%error, "Discovery HTTP client unavailable");
                None
            }
        }
    }

    pub async fn candidates(&self, viewer_id: u64) -> Result<Vec<Plan>, String> {
        self.post("/candidates", &serde_json::json!({ "viewer_id": viewer_id }))
            .await
    }

    pub async fn offer(
        &self,
        viewer_id: u64,
        request_id: u64,
        slots: usize,
        posts: &[OfferPost],
    ) -> Result<Vec<u64>, String> {
        self.post(
            "/offer",
            &serde_json::json!({
                "viewer_id": viewer_id,
                "request_id": request_id,
                "slots": slots,
                "posts": posts,
            }),
        )
        .await
    }

    async fn post<T: serde::de::DeserializeOwned>(
        &self,
        path: &str,
        body: &serde_json::Value,
    ) -> Result<T, String> {
        self.http
            .post(format!("{}{path}", self.base))
            .bearer_auth(&self.token)
            .json(body)
            .send()
            .await
            .and_then(reqwest::Response::error_for_status)
            .map_err(|error| error.to_string())?
            .json()
            .await
            .map_err(|error| error.to_string())
    }
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub(crate) struct Plan {
    pub post_id: u64,
    pub author_id: u64,
    pub stage: u8,
}

#[derive(Clone, Copy, Debug, Serialize)]
pub(crate) struct OfferPost {
    pub post_id: u64,
    pub stage: u8,
}

#[derive(Debug, Default)]
pub struct DiscoverySlate {
    pub(crate) graph_checks: std::sync::atomic::AtomicU8,
    pub(crate) plans: OnceLock<HashMap<u64, (usize, Plan)>>,
    pub(crate) selected: OnceLock<Vec<OfferPost>>,
}

pub(crate) const BLOCKS_CHECKED: u8 = 1;
pub(crate) const MUTES_CHECKED: u8 = 2;
pub(crate) const FOLLOWS_CHECKED: u8 = 4;
