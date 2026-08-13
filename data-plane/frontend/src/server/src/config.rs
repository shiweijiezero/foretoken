// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Server configuration.

use std::net::{SocketAddr, ToSocketAddrs};

/// Address and port the HTTP frontend binds to.
///
/// `port: 0` requests an ephemeral port chosen by the OS, useful for tests.
#[derive(Debug, Clone)]
pub struct ServerConfig {
    pub host: String,
    pub port: u16,
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            host: "0.0.0.0".to_string(),
            port: 8000,
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
