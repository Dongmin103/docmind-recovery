#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
#  AFTER UPDATING THIS FILE, PLEASE ENSURE THAT docs/references/supported_models.mdx IS ALSO UPDATED for consistency!
#

from enum import StrEnum


class SupportedLiteLLMProvider(StrEnum):
    OpenAI = "OpenAI"


FACTORY_DEFAULT_BASE_URL = {SupportedLiteLLMProvider.OpenAI: "https://api.openai.com/v1"}
LITELLM_PROVIDER_PREFIX = {SupportedLiteLLMProvider.OpenAI: "openai/"}

from .chat_model import LiteLLMBase, OpenAI_APIChat
from .cv_model import GptV4, OpenAI_APICV
from .embedding_model import OpenAIEmbed, OpenAI_APIEmbed
from .rerank_model import JinaRerank, KURERerank
from .model_meta import OpenAIAPICompatible

ChatModel = {"OpenAI": LiteLLMBase, "OpenAI-API-Compatible": OpenAI_APIChat}
CvModel = {"OpenAI": GptV4, "OpenAI-API-Compatible": OpenAI_APICV}
EmbeddingModel = {"OpenAI": OpenAIEmbed, "OpenAI-API-Compatible": OpenAI_APIEmbed}
RerankModel = {"Jina": JinaRerank, "KURE": KURERerank}
ModelMeta = {"OpenAI-API-Compatible": OpenAIAPICompatible}

SUPPORTED_MODEL_TYPES = {
    "OpenAI": frozenset({"chat", "embedding", "vision"}),
    "OpenAI-API-Compatible": frozenset({"chat", "embedding", "vision"}),
    "Jina": frozenset({"rerank"}),
    "KURE": frozenset({"rerank"}),
}


def validate_model_selection(provider, model_types):
    supported = SUPPORTED_MODEL_TYPES.get(provider)
    if supported is None:
        raise ValueError(f"Unsupported provider: {provider}")
    if isinstance(model_types, str):
        model_types = [model_types]
    if not model_types or any(mt not in supported for mt in model_types):
        raise ValueError(f"Unsupported model type for {provider}: {model_types}")


__all__ = ["ChatModel", "CvModel", "EmbeddingModel", "RerankModel", "ModelMeta"]
