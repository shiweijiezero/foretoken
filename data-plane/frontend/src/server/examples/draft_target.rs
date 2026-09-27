// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Run the frontend DT workflow against two existing role services using token-input JSON.

use foretoken_llm_facade::draft_target::RoleClient;
use foretoken_model_protocol::{GenerateInput, TokenOutput};
use foretoken_server::draft_target::generate_draft_target_tokens;
use futures::StreamExt;
use std::io::{self, Read};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args.len() != 2 {
        return Err("usage: draft_target DRAFT_URL TARGET_URL < request.json".into());
    }
    let mut input = String::new();
    io::stdin().read_to_string(&mut input)?;
    let request: GenerateInput = serde_json::from_str(&input)?;
    let mut stream = generate_draft_target_tokens(
        RoleClient::new(args[0].clone())?,
        RoleClient::new(args[1].clone())?,
        request.into(),
    )
    .await?;
    loop {
        tokio::select! {
            result = tokio::signal::ctrl_c() => { result?; break; }
            event = stream.next() => match event {
                Some(event) => println!("{}", serde_json::to_string(&TokenOutput::from(event?))?),
                None => break,
            }
        }
    }
    Ok(())
}
