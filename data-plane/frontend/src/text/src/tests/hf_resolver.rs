use std::path::Path;
use std::sync::{Arc, Mutex};

use crate::{Prompt, TextBackend, TextRequest, TextRequestProcessor};
use foretoken_tokenizer::{DynTokenizer, Result, Tokenizer};
use hf_hub::api::Siblings;

use super::{cache_snapshot_dir, files_for_local_hf_resolver};

#[derive(Default)]
struct CountingTokenizer(Mutex<usize>);

impl Tokenizer for CountingTokenizer {
    fn encode(&self, text: &str, _: bool) -> Result<Vec<u32>> {
        *self.0.lock().unwrap() += 1;
        Ok(text.bytes().map(u32::from).collect())
    }
    fn encode_ordinary(&self, text: &str) -> Result<Vec<u32>> {
        self.encode(text, false)
    }
    fn decode(&self, _: &[u32], _: bool) -> Result<String> {
        Ok(String::new())
    }
    fn token_to_id(&self, _: &str) -> Option<u32> {
        None
    }
    fn id_to_token(&self, _: u32) -> Option<String> {
        None
    }
    fn vocab_size(&self) -> usize {
        512
    }
}

struct CountingBackend(Arc<CountingTokenizer>);

impl TextBackend for CountingBackend {
    fn tokenizer(&self) -> DynTokenizer {
        self.0.clone()
    }

    fn model_id(&self) -> &str {
        "test"
    }

    fn model_vocab_size(&self) -> usize {
        512
    }
}

#[test]
fn selects_the_remote_files_needed_by_vllms_local_resolver() {
    let siblings = [
        "README.md",
        "tokenizer_config.json",
        "config.json",
        "generation_config.json",
        "preprocessor_config.json",
        "processor_config.json",
        "chat_template.json",
        "chat_template.jinja",
        "custom-chat.jinja",
        "tekken.json",
        "tokenizer.json",
        "tiktoken.model",
        "special.tiktoken",
    ]
    .into_iter()
    .map(|rfilename| Siblings {
        rfilename: rfilename.into(),
    })
    .collect::<Vec<_>>();

    assert_eq!(
        files_for_local_hf_resolver(&siblings),
        [
            "chat_template.jinja",
            "chat_template.json",
            "config.json",
            "custom-chat.jinja",
            "generation_config.json",
            "preprocessor_config.json",
            "processor_config.json",
            "special.tiktoken",
            "tekken.json",
            "tiktoken.model",
            "tokenizer.json",
            "tokenizer_config.json",
        ]
    );
}

#[test]
fn resolves_the_snapshot_root_for_nested_artifacts() {
    assert_eq!(
        cache_snapshot_dir(Path::new(
            "/cache/models--org--repo/snapshots/revision/nested/tokenizer.tiktoken"
        )),
        Some(Path::new("/cache/models--org--repo/snapshots/revision").to_path_buf())
    );
}

#[test]
fn uses_vllm_lowering_after_exactly_one_tokenization() {
    let tokenizer = Arc::new(CountingTokenizer::default());
    let processor = TextRequestProcessor::new(Arc::new(CountingBackend(tokenizer.clone())), 1024);
    let mut request = TextRequest::for_test();
    request.prompt = Prompt::Text("abc".into());

    let prepared = processor.prepare(request).unwrap();

    assert_eq!(prepared.generate_request.prompt_token_ids, vec![97, 98, 99]);
    assert_eq!(*tokenizer.0.lock().unwrap(), 1);
}
