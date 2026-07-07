from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from app.config import get_settings

settings = get_settings()

_KIMI_MODELS = ("moonshot", "kimi")


def get_llm(model_str: str = None):
    model_str = model_str or settings.default_model
    provider, model = model_str.split("/", 1)
    provider = provider.lower()

    if provider == "openai":
        # Modelos Kimi/Moonshot usan API compatible con OpenAI pero distinta base_url
        if model.lower().startswith(_KIMI_MODELS):
            if not settings.kimi_api_key:
                raise ValueError("KIMI_API_KEY no configurada")
            # kimi-k2.x solo acepta temperature=1; moonshot acepta cualquier valor
            temp = 1 if model.lower().startswith("kimi-k2") else 0.2
            return ChatOpenAI(
                model=model,
                api_key=settings.kimi_api_key,
                base_url=settings.kimi_base_url,
                temperature=temp,
            )
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY no configurada")
        return ChatOpenAI(
            model=model,
            api_key=settings.openai_api_key,
            temperature=0.2,
        )

    elif provider == "anthropic":
        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY no configurada")
        return ChatAnthropic(
            model=model,
            api_key=settings.anthropic_api_key,
            temperature=0.2,
        )

    elif provider == "google":
        if not settings.google_api_key:
            raise ValueError("GOOGLE_API_KEY no configurada")
        return ChatGoogleGenerativeAI(
            model=model,
            google_api_key=settings.google_api_key,
            temperature=0.2,
        )

    elif provider == "kimi":
        if not settings.kimi_api_key:
            raise ValueError("KIMI_API_KEY no configurada")
        return ChatOpenAI(
            model=model,
            api_key=settings.kimi_api_key,
            base_url=settings.kimi_base_url,
            temperature=0.2,
        )

    else:
        raise ValueError(f"Proveedor no soportado: {provider}")


def list_available_models():
    return {
        "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"],
        "anthropic": ["claude-3-5-sonnet-20241022", "claude-3-haiku-20240307", "claude-3-opus-20240229"],
        "google": ["gemini-1.5-pro-latest", "gemini-1.5-flash-latest", "gemini-1.0-pro"],
        "kimi": [
            "moonshot-v1-auto",
            "moonshot-v1-128k",
            "moonshot-v1-32k",
            "moonshot-v1-8k",
            "kimi-k1-5",
            "kimi-k1-5-turbo",
        ],
    }
