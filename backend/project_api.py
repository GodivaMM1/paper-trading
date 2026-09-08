"""Authenticated project-memory HTTP routes for the cloud handler."""
import hmac
import json
import os
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from backend.project_memory import ConflictError, ProjectMemory

PREFIX = '/api/project/'


def install(handler, db_path):
    memory = ProjectMemory(db_path)
    previous_get, previous_post = handler.do_GET, handler.do_POST

    def authorized(self):
        token = os.environ.get('PAPER_TRADING_API_TOKEN', '').strip()
        supplied = self.headers.get('X-Admin-Token', '')
        if not token:
            self._json({'error': 'project API token is not configured'}, 503)
            return False
        if not hmac.compare_digest(token.encode(), supplied.encode()):
            self._json({'error': 'unauthorized'}, 401)
            return False
        return True

    def get(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith(PREFIX):
            return previous_get(self)
        if not authorized(self):
            return
        try:
            if parsed.path == PREFIX + 'records':
                query = parse_qs(parsed.query)
                return self._json(memory.list(query.get('after', ['0'])[-1], query.get('limit', ['100'])[-1]))
            if parsed.path == PREFIX + 'context':
                self._json(project_context(self, memory))
                return
            self._json({'error': 'not found'}, 404)
        except ValueError as exc:
            self._json({'error': str(exc)}, 400)
        except Exception:
            self._json({'error': 'project read failed'}, 500)

    def post(self):
        path = urlparse(self.path).path
        if not path.startswith(PREFIX):
            return previous_post(self)
        if not authorized(self):
            return
        if path != PREFIX + 'records':
            return self._json({'error': 'not found'}, 404)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 65536:
                return self._json({'error': 'body must be 1..65536 bytes'}, 413)
            payload = json.loads(self.rfile.read(length).decode('utf-8'))
            record, created = memory.append(payload)
            self._json({'record': record, 'created': created}, 201 if created else 200)
        except ConflictError as exc:
            self._json({'error': str(exc)}, 409)
        except (ValueError, UnicodeError) as exc:
            self._json({'error': str(exc)}, 400)
        except Exception:
            self._json({'error': 'project write failed'}, 500)

    handler.do_GET, handler.do_POST = get, post


def project_context(handler, memory):
    account_id = 'acct_588000_grid'
    account = handler.trading.get_account(account_id)
    records = memory.current()
    return {
                    'schema_version': 1, 'project_id': 'simulation-investing',
                    'retrieved_at': datetime.now(timezone.utc).isoformat(),
                    'account_type': 'paper', 'account': account,
                    'positions': handler.trading.list_positions(account_id) if account else [],
                    'recent_trade_events': handler.store.list_events({
                        'account_id': account_id, 'event_type': 'trade_filled', 'limit': 50}),
                    'active_records': records,
                    'market_quote': None,
                    'data_quality': {
                        'account_available': account is not None,
                        'historical_screenshot_provenance': 'not_reverified_by_project_memory',
                        'quote_status': 'not_fetched',
                        'strategy_status': 'recorded_notes_only_not_execution_configuration',
                        'recent_trade_limit': 50,
                    },
                    'capabilities': {'record_memory': True, 'record_decision': True,
                                     'record_review': True, 'candidate_learning': True,
                                     'automatic_evaluation': False, 'automatic_skill_promotion': False},
                }
