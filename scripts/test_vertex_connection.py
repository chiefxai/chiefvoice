import asyncio
import sys
from chiefvoice.core.config import settings
from chiefvoice.identity.api_keys import SqlApiKeyDirectory
from pipecat.services.google.gemini_live.vertex.llm import GeminiLiveVertexLLMService


async def test_vertex(key_id: str):
    store = SqlApiKeyDirectory(settings.database_url, settings.credentials_encryption_key)
    config = await store.get_vertex_config(key_id)
    await store.close()

    if not config:
        print(f"ERROR: No vertex config found for key {key_id}")
        return

    print("--- Vertex Config ---")
    print(f"Project ID: {config.project_id}")
    print(f"Location: {config.location}")
    print(f"Voice Model: {config.voice_model}")
    print(f"Has credentials_json: {bool(config.credentials_json)}")

    print("\n--- Initializing GeminiLiveVertexLLMService ---")
    try:
        service = GeminiLiveVertexLLMService(
            project_id=config.project_id,
            location=config.location,
            credentials=config.credentials_json,
            model=config.voice_model,
            voice_id="Charon",
        )
        print("Service created successfully.")
    except Exception as exc:
        print(f"Failed to create service: {type(exc).__name__}: {exc}")
        return

    print("\n--- Testing Live API Connection ---")
    try:
        service.create_client()
        client = service._client
        print(f"Client created: {client}")
        # Test connecting
        from pipecat.services.google.gemini_live.llm import LiveConnectConfig
        from google.genai.types import Modality, SpeechConfig, VoiceConfig, PrebuiltVoiceConfig, GenerationConfig

        cfg = LiveConnectConfig(
            generation_config=GenerationConfig(
                response_modalities=[Modality.AUDIO],
                speech_config=SpeechConfig(
                    voice_config=VoiceConfig(
                        prebuilt_voice_config=PrebuiltVoiceConfig(voice_name="Charon")
                    )
                ),
            )
        )
        print(f"Attempting live connect to model '{config.voice_model}' in '{config.location}'...")
        async with client.aio.live.connect(model=config.voice_model, config=cfg) as session:
            print("SUCCESS! Connected to Gemini Live on Vertex AI!")
            turn = session.receive()
            print("Session active, closing test connection.")
    except Exception as exc:
        print(f"\nFAILED to connect: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    k_id = sys.argv[1] if len(sys.argv) > 1 else "b92bdfb222a05ab4b870083d"
    asyncio.run(test_vertex(k_id))
