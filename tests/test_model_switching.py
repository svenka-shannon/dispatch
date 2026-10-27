"""
Tests for per-chat model switching.

Covers:
- Model parameter passed through SDKSession
- Model resolution in SDKBackend (registry override vs default)
- Model persistence in registry
- set_model IPC command
- Model preserved across session restart
"""
import asyncio
from datetime import datetime
from unittest.mock import patch, AsyncMock

import pytest
import pytest_asyncio


@pytest.mark.asyncio
class TestSDKSessionModel:
    """Test SDKSession model parameter."""

    async def test_session_default_model_is_opus(self, sdk_session):
        """Sessions should default to opus model."""
        assert sdk_session.model == "opus"

    async def test_session_accepts_custom_model(self, tmp_path):
        """Sessions should accept custom model parameter."""
        import os
        from assistant.sdk_session import SDKSession

        cwd = str(tmp_path / "test-session")
        os.makedirs(cwd, exist_ok=True)

        session = SDKSession(
            chat_id="test:+15555551234",
            contact_name="Test User",
            tier="admin",
            cwd=cwd,
            session_type="individual",
            source="test",
            model="sonnet",
        )
        assert session.model == "sonnet"

    async def test_session_model_used_in_options(self, tmp_path):
        """Model should be used in _build_options."""
        import os
        from assistant.sdk_session import SDKSession

        cwd = str(tmp_path / "test-session")
        os.makedirs(cwd, exist_ok=True)

        session = SDKSession(
            chat_id="test:+15555551234",
            contact_name="Test User",
            tier="admin",
            cwd=cwd,
            session_type="individual",
            source="test",
            model="haiku",
        )

        opts = session._build_options()
        # Asserts the RESOLVED id, not the alias. These asserted "haiku"/"opus"
        # from before _resolve_model_and_betas existed, so they failed on every
        # run regardless of which model was configured — a permanently-red test
        # cannot catch a model regression.
        assert opts.model == "claude-haiku-4-5"

    async def test_session_opus_in_options(self, sdk_session):
        """Default opus model should be in options."""
        opts = sdk_session._build_options()
        from assistant.sdk_session import _MODEL_ALIASES
        assert opts.model == _MODEL_ALIASES["opus"]


@pytest.mark.asyncio
class TestSDKBackendModelResolution:
    """Test model resolution in SDKBackend."""

    async def test_create_session_defaults_to_opus(self, sdk_backend):
        """New sessions should default to opus."""
        session = await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )
        assert session.model == "opus"

    async def test_create_session_uses_registry_model(self, sdk_backend, registry):
        """Sessions should use model from registry if set."""
        # Pre-register with a custom model
        registry.register(
            chat_id="test:+15555551234",
            session_name="test/_15555551234",
            model="sonnet",
            tier="admin",
        )

        session = await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )
        assert session.model == "sonnet"

    async def test_create_session_preserves_registry_model(self, sdk_backend, registry):
        """Creating session should preserve existing registry model."""
        # Pre-register with a custom model
        registry.register(
            chat_id="test:+15555551234",
            session_name="test/_15555551234",
            model="haiku",
            tier="admin",
        )

        await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )

        # Check registry still has model
        reg = registry.get("test:+15555551234")
        assert reg["model"] == "haiku"

    async def test_new_session_registers_opus_default(self, sdk_backend, registry):
        """New sessions should register with opus as default model."""
        await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )

        reg = registry.get("test:+15555551234")
        assert reg["model"] == "opus"


@pytest.mark.asyncio
class TestModelPersistence:
    """Test model persistence across session restarts."""

    async def test_restart_session_preserves_model(self, sdk_backend, registry):
        """Restarting session should preserve the model from registry."""
        # Pre-register with custom model
        registry.register(
            chat_id="test:+15555551234",
            session_name="test/_15555551234",
            model="sonnet",
            tier="admin",
            contact_name="Test User",
            source="test",
        )

        # Create initial session
        s1 = await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )
        assert s1.model == "sonnet"

        # Restart
        s2 = await sdk_backend.restart_session("test:+15555551234")
        assert s2.model == "sonnet"

    async def test_model_survives_kill_and_recreate(self, sdk_backend, registry):
        """Model should persist in registry after session is killed."""
        # Set model in registry
        registry.register(
            chat_id="test:+15555551234",
            session_name="test/_15555551234",
            model="haiku",
            tier="admin",
        )

        # Create and kill session
        await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )
        await sdk_backend.kill_session("test:+15555551234")

        # Registry should still have model
        reg = registry.get("test:+15555551234")
        assert reg["model"] == "haiku"

        # Recreate session
        s2 = await sdk_backend.create_session(
            "Test User", "test:+15555551234", "admin", source="test"
        )
        assert s2.model == "haiku"


@pytest.mark.asyncio
class TestUnlockedSessionCreation:
    """Test _create_session_unlocked model handling."""

    async def test_unlocked_creation_uses_registry_model(self, sdk_backend, registry):
        """_create_session_unlocked should also use registry model."""
        # Pre-register with custom model
        registry.register(
            chat_id="test:+15555551234",
            session_name="test/_15555551234",
            model="sonnet",
            tier="admin",
        )

        async with sdk_backend._lock:
            session = await sdk_backend._create_session_unlocked(
                "Test User", "test:+15555551234", "admin", source="test"
            )

        assert session.model == "sonnet"


@pytest.mark.asyncio
class TestInjectMessageModel:
    """Test inject_message uses correct model."""

    async def test_inject_creates_session_with_registry_model(self, sdk_backend, registry):
        """inject_message creating session should use registry model."""
        # Pre-register with custom model
        registry.register(
            chat_id="test:+15555551234",
            session_name="test/_15555551234",
            model="haiku",
            tier="admin",
        )

        await sdk_backend.inject_message(
            "Test User", "+15555551234", "hello",
            tier="admin", source="test",
        )

        session = sdk_backend.sessions["test:+15555551234"]
        assert session.model == "haiku"


@pytest.mark.asyncio
class TestAllTiersDefaultOpus:
    """Test that all tiers default to opus (per requirement)."""

    @pytest.mark.parametrize("tier", ["admin", "partner", "family", "favorite", "unknown"])
    async def test_tier_defaults_to_opus(self, sdk_backend, tier):
        """All tiers should default to opus model."""
        session = await sdk_backend.create_session(
            "Test User", f"test:+1555555{tier[:4]}", tier, source="test"
        )
        assert session.model == "opus"
