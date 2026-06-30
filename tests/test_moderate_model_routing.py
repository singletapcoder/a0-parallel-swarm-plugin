"""Unit test for moderate_model routing enhancement."""
import pytest
from unittest.mock import Mock
from plugins.parallel_swarm.python.helpers.model_router import TaskComplexity, select_model_config


def test_moderate_model_routing_when_configured():
    """Test that MODERATE tasks use swarm_model_moderate when configured."""
    # Setup mock config with moderate_model override
    mock_config = Mock()
    mock_config.chat_model = "default-model"
    mock_config.swarm_model_simple = "simple-model"
    mock_config.swarm_model_moderate = "moderate-model"
    mock_config.swarm_model_complex = "complex-model"
    
    # Test MODERATE routing
    result = select_model_config(TaskComplexity.MODERATE, mock_config)
    
    assert result.chat_model == "moderate-model"
    assert result != mock_config  # Should be a copy, not the original


def test_moderate_model_routing_when_not_configured():
    """Test that MODERATE tasks use default model when moderate_model not set."""
    # Setup mock config without moderate_model override
    mock_config = Mock()
    mock_config.chat_model = "default-model"
    mock_config.swarm_model_simple = "simple-model"
    mock_config.swarm_model_moderate = ""  # Empty string = not configured
    mock_config.swarm_model_complex = "complex-model"
    
    # Test MODERATE routing falls back to default
    result = select_model_config(TaskComplexity.MODERATE, mock_config)
    
    assert result.chat_model == "default-model"
    assert result == mock_config  # Should return original config


def test_simple_complex_routing_still_works():
    """Test that SIMPLE and COMPLEX routing still works with moderate_model added."""
    mock_config = Mock()
    mock_config.chat_model = "default-model"
    mock_config.swarm_model_simple = "simple-model"
    mock_config.swarm_model_moderate = "moderate-model"
    mock_config.swarm_model_complex = "complex-model"
    
    # Test SIMPLE routing
    simple_result = select_model_config(TaskComplexity.SIMPLE, mock_config)
    assert simple_result.chat_model == "simple-model"
    
    # Test COMPLEX routing
    complex_result = select_model_config(TaskComplexity.COMPLEX, mock_config)
    assert complex_result.chat_model == "complex-model"


def test_all_three_tiers_independent():
    """Test that all three complexity tiers can be configured independently."""
    mock_config = Mock()
    mock_config.chat_model = "default-model"
    mock_config.swarm_model_simple = "openrouter/z-ai/glm-5.2"
    mock_config.swarm_model_moderate = "openrouter/google/gemini-2.5-flash"
    mock_config.swarm_model_complex = "openrouter/anthropic/claude-sonnet-4"
    
    simple = select_model_config(TaskComplexity.SIMPLE, mock_config)
    moderate = select_model_config(TaskComplexity.MODERATE, mock_config)
    complex = select_model_config(TaskComplexity.COMPLEX, mock_config)
    
    assert simple.chat_model == "openrouter/z-ai/glm-5.2"
    assert moderate.chat_model == "openrouter/google/gemini-2.5-flash"
    assert complex.chat_model == "openrouter/anthropic/claude-sonnet-4"
    
    # All should be copies, not the original
    assert simple != mock_config
    assert moderate != mock_config
    assert complex != mock_config
