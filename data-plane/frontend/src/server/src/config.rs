// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Server configuration.

use std::net::{SocketAddr, ToSocketAddrs};

/// Address, port, and downstream model-server the HTTP frontend talks to.
///
/// `port: 0` requests an ephemeral port chosen by the OS, useful for tests.
#[derive(Debug, Clone)]
pub struct ServerConfig {
    pub host: String,
    pub port: u16,
    pub model_server_url: String,
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            host: "0.0.0.0".to_string(),
            port: 8000,
            model_server_url: "http://127.0.0.1:9000".to_string(),
        }
    }
}

impl ServerConfig {
    /// Resolve the configured host and port to a socket address.
    pub fn socket_addr(&self) -> SocketAddr {
        (self.host.as_str(), self.port)
            .to_socket_addrs()
            .expect("host should resolve to a socket address")
            .next()
            .expect("host should resolve to at least one address")
    }
}
