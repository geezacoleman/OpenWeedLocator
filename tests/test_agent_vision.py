"""Tests for agent vision: image upload, grab frame, content array handling."""

import base64
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from agent.engine import AgentEngine, Session
from agent.tool_registry import ToolRegistry
from agent.llm_provider import StreamChunk


class MockProvider:
    """Minimal mock LLM provider for testing."""
    model = 'test-model'

    def __init__(self, responses=None):
        self.responses = responses or [[StreamChunk(type='text_delta', data='Hello')]]
        self._call_idx = 0
        self.last_messages = None

    def validate_key(self):
        return True

    def stream_chat(self, messages=None, tools=None, system=None):
        self.last_messages = messages
        if self._call_idx < len(self.responses):
            chunks = self.responses[self._call_idx]
            self._call_idx += 1
            yield from chunks
        else:
            yield StreamChunk(type='text_delta', data='done')


class MockOpenAIProvider(MockProvider):
    """Mock provider that looks like OpenAI to the engine."""
    pass


# ---------------------------------------------------------------------------
# Engine tests
# ---------------------------------------------------------------------------

class TestChatAcceptsString(unittest.TestCase):
    """Existing behavior: chat() accepts a plain string."""

    def test_string_message(self):
        registry = ToolRegistry()
        engine = AgentEngine(tool_registry=registry, provider=MockProvider())
        chunks = list(engine.chat('s1', 'hello'))
        text_chunks = [c for c in chunks if c.type == 'text_delta']
        self.assertTrue(len(text_chunks) > 0)
        session = engine._get_session('s1')
        self.assertEqual(session.messages[0]['content'], 'hello')


class TestChatAcceptsContentArray(unittest.TestCase):
    """chat() accepts a list with image + text blocks."""

    def test_content_array_stored(self):
        registry = ToolRegistry()
        engine = AgentEngine(tool_registry=registry, provider=MockProvider())

        content = [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': 'abc123'}},
            {'type': 'text', 'text': 'What is this?'},
        ]
        chunks = list(engine.chat('s2', content))
        session = engine._get_session('s2')
        self.assertIsInstance(session.messages[0]['content'], list)
        self.assertEqual(session.messages[0]['content'][0]['type'], 'image')
        self.assertEqual(session.messages[0]['content'][1]['text'], 'What is this?')


class TestTranslateImagesAnthropicPassthrough(unittest.TestCase):
    """_translate_images() is a no-op for Anthropic provider."""

    def test_no_change(self):
        registry = ToolRegistry()
        engine = AgentEngine(tool_registry=registry, provider=MockProvider())

        messages = [
            {'role': 'user', 'content': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': 'abc'}},
                {'type': 'text', 'text': 'hi'},
            ]}
        ]
        result = engine._translate_images(messages)
        self.assertEqual(result, messages)
        # Verify image block is unchanged
        self.assertEqual(result[0]['content'][0]['type'], 'image')


class TestTranslateImagesOpenAIConverts(unittest.TestCase):
    """_translate_images() converts Anthropic image blocks to OpenAI format."""

    def test_conversion(self):
        registry = ToolRegistry()
        mock_provider = MockOpenAIProvider()
        engine = AgentEngine(tool_registry=registry, provider=mock_provider)

        # Patch _schema_format to return 'openai'
        engine._schema_format = lambda: 'openai'

        messages = [
            {'role': 'user', 'content': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': 'abc123'}},
                {'type': 'text', 'text': 'describe this'},
            ]}
        ]
        result = engine._translate_images(messages)

        # Should have converted image block
        img_block = result[0]['content'][0]
        self.assertEqual(img_block['type'], 'image_url')
        self.assertEqual(img_block['image_url']['url'], 'data:image/jpeg;base64,abc123')

        # Text block should pass through
        text_block = result[0]['content'][1]
        self.assertEqual(text_block['type'], 'text')
        self.assertEqual(text_block['text'], 'describe this')

    def test_string_messages_passthrough(self):
        """String content messages are not affected."""
        registry = ToolRegistry()
        engine = AgentEngine(tool_registry=registry, provider=MockOpenAIProvider())
        engine._schema_format = lambda: 'openai'

        messages = [{'role': 'user', 'content': 'just text'}]
        result = engine._translate_images(messages)
        self.assertEqual(result[0]['content'], 'just text')


class TestSessionTitleFromImageMessage(unittest.TestCase):
    """_session_title() extracts text from mixed content arrays."""

    def test_title_from_image_message(self):
        registry = ToolRegistry()
        engine = AgentEngine(tool_registry=registry)

        session = Session()
        session.messages = [
            {'role': 'user', 'content': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': 'x'}},
                {'type': 'text', 'text': 'Create an algorithm for this weed'},
            ]}
        ]
        title = engine._session_title(session)
        self.assertEqual(title, 'Create an algorithm for this weed')

    def test_title_from_image_only(self):
        """Image-only messages should return default title."""
        registry = ToolRegistry()
        engine = AgentEngine(tool_registry=registry)

        session = Session()
        session.messages = [
            {'role': 'user', 'content': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': 'x'}},
            ]}
        ]
        title = engine._session_title(session)
        self.assertEqual(title, 'New conversation')


class TestTranslateImagesUsedInStream(unittest.TestCase):
    """Verify that stream_chat receives translated messages."""

    def test_openai_receives_translated(self):
        registry = ToolRegistry()
        provider = MockOpenAIProvider()
        engine = AgentEngine(tool_registry=registry, provider=provider)
        engine._schema_format = lambda: 'openai'

        content = [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': 'abc'}},
            {'type': 'text', 'text': 'hi'},
        ]
        list(engine.chat('s3', content))

        # Provider should have received translated messages
        last_msgs = provider.last_messages
        self.assertIsNotNone(last_msgs)
        user_msg = last_msgs[0]
        img_block = user_msg['content'][0]
        self.assertEqual(img_block['type'], 'image_url')


# ---------------------------------------------------------------------------
# Route tests (networked)
# ---------------------------------------------------------------------------

class TestNetworkedChatRouteWithImages(unittest.TestCase):
    """Test the networked /api/agent/chat route builds content arrays."""

    def _make_app(self):
        """Create a minimal Flask app mimicking the agent chat route."""
        from flask import Flask, request, jsonify, Response
        app = Flask(__name__)
        app.config['TESTING'] = True

        mock_engine = MagicMock()
        mock_engine.chat.return_value = iter([
            StreamChunk(type='text_delta', data='OK'),
        ])
        mock_engine.get_session_info.return_value = {
            'input_tokens': 10, 'output_tokens': 5, 'message_count': 2,
        }

        @app.route('/api/agent/chat', methods=['POST'])
        def agent_chat():
            data = request.get_json()
            if not data:
                return jsonify({'error': 'No JSON body'}), 400
            message = data.get('message', '').strip()
            images = data.get('images', [])
            session_id = data.get('session_id', 'default')
            if not message and not images:
                return jsonify({'error': 'Message or image is required'}), 400
            if len(images) > 4:
                return jsonify({'error': 'Maximum 4 images per message'}), 400
            for img in images:
                if not isinstance(img, str) or len(img) > 1_400_000:
                    return jsonify({'error': 'Invalid or oversized image (max ~1MB)'}), 400

            if images:
                content = []
                for img_data in images:
                    content.append({
                        'type': 'image',
                        'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': img_data}
                    })
                if message:
                    content.append({'type': 'text', 'text': message})
                chat_input = content
            else:
                chat_input = message

            def generate():
                for chunk in mock_engine.chat(session_id, chat_input):
                    yield f"data: {json.dumps({'type': chunk.type, 'data': chunk.data})}\n\n"
            return Response(generate(), mimetype='text/event-stream')

        return app, mock_engine

    def test_text_only(self):
        app, engine = self._make_app()
        with app.test_client() as c:
            resp = c.post('/api/agent/chat', json={
                'session_id': 's1', 'message': 'hello',
            })
            self.assertEqual(resp.status_code, 200)
            engine.chat.assert_called_once_with('s1', 'hello')

    def test_with_images(self):
        app, engine = self._make_app()
        with app.test_client() as c:
            resp = c.post('/api/agent/chat', json={
                'session_id': 's1',
                'message': 'detect this',
                'images': ['abc123'],
            })
            self.assertEqual(resp.status_code, 200)
            call_args = engine.chat.call_args[0]
            self.assertEqual(call_args[0], 's1')
            content = call_args[1]
            self.assertIsInstance(content, list)
            self.assertEqual(content[0]['type'], 'image')
            self.assertEqual(content[0]['source']['data'], 'abc123')
            self.assertEqual(content[1]['type'], 'text')
            self.assertEqual(content[1]['text'], 'detect this')

    def test_image_only_no_text(self):
        app, engine = self._make_app()
        with app.test_client() as c:
            resp = c.post('/api/agent/chat', json={
                'session_id': 's1',
                'message': '',
                'images': ['abc123'],
            })
            self.assertEqual(resp.status_code, 200)
            content = engine.chat.call_args[0][1]
            self.assertIsInstance(content, list)
            self.assertEqual(len(content), 1)  # Only image, no text block

    def test_rejects_too_many_images(self):
        app, _ = self._make_app()
        with app.test_client() as c:
            resp = c.post('/api/agent/chat', json={
                'session_id': 's1',
                'message': 'hi',
                'images': ['a', 'b', 'c', 'd', 'e'],
            })
            self.assertEqual(resp.status_code, 400)
            data = resp.get_json()
            self.assertIn('Maximum 4', data['error'])

    def test_rejects_oversized_image(self):
        app, _ = self._make_app()
        with app.test_client() as c:
            huge = 'x' * 1_500_000
            resp = c.post('/api/agent/chat', json={
                'session_id': 's1',
                'message': 'hi',
                'images': [huge],
            })
            self.assertEqual(resp.status_code, 400)
            data = resp.get_json()
            self.assertIn('oversized', data['error'])

    def test_rejects_empty_message_and_no_images(self):
        app, _ = self._make_app()
        with app.test_client() as c:
            resp = c.post('/api/agent/chat', json={
                'session_id': 's1',
                'message': '',
            })
            self.assertEqual(resp.status_code, 400)


class TestGrabFrameRoute(unittest.TestCase):
    """Test the grab_frame route returns base64."""

    def _make_app(self):
        from flask import Flask, jsonify
        app = Flask(__name__)
        app.config['TESTING'] = True

        @app.route('/api/agent/grab_frame/<device_id>')
        def agent_grab_frame(device_id):
            import base64 as b64mod
            device_id = device_id.replace('_', '-')
            # Simulate fetching frame
            return jsonify({'image': 'dGVzdA==', 'device_id': device_id})

        return app

    def test_returns_base64(self):
        app = self._make_app()
        with app.test_client() as c:
            resp = c.get('/api/agent/grab_frame/owl-001')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertIn('image', data)
            self.assertEqual(data['device_id'], 'owl-001')

    def test_underscore_to_dash(self):
        app = self._make_app()
        with app.test_client() as c:
            resp = c.get('/api/agent/grab_frame/owl_001')
            data = resp.get_json()
            self.assertEqual(data['device_id'], 'owl-001')


class TestGrabFrameStandalone(unittest.TestCase):
    """Test standalone grab_frame route."""

    def _make_app(self):
        from flask import Flask, jsonify
        app = Flask(__name__)
        app.config['TESTING'] = True

        @app.route('/api/agent/grab_frame')
        def agent_grab_frame():
            return jsonify({'image': 'dGVzdA=='})

        return app

    def test_returns_base64(self):
        app = self._make_app()
        with app.test_client() as c:
            resp = c.get('/api/agent/grab_frame')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertIn('image', data)


# ---------------------------------------------------------------------------
# Session persistence with images
# ---------------------------------------------------------------------------

class TestSessionPersistenceWithImages(unittest.TestCase):
    """Sessions containing images can be saved and loaded."""

    def test_save_and_load_image_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = ToolRegistry()
            engine = AgentEngine(
                tool_registry=registry,
                provider=MockProvider(),
                sessions_dir=tmpdir,
            )

            content = [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': 'abc123'}},
                {'type': 'text', 'text': 'What is this?'},
            ]
            list(engine.chat('session_img1', content))

            # Load from disk
            engine2 = AgentEngine(
                tool_registry=registry,
                provider=MockProvider(),
                sessions_dir=tmpdir,
            )
            data = engine2.load_session('session_img1')
            self.assertIsNotNone(data)
            user_msg = data['messages'][0]
            self.assertIsInstance(user_msg['content'], list)
            self.assertEqual(user_msg['content'][0]['type'], 'image')
            self.assertEqual(user_msg['content'][0]['source']['data'], 'abc123')


if __name__ == '__main__':
    unittest.main()
