import json
import unittest
from unittest.mock import patch

from boto3.dynamodb.conditions import Key

from handler import lambda_handler


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, *, Item, **kwargs):
        self.items[(Item["PK"], Item["SK"])] = Item

    def query(self, *, KeyConditionExpression, ConsistentRead):
        del KeyConditionExpression, ConsistentRead
        return {"Items": list(self.items.values())}


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable()
        self.patch = patch("handler._get_table", return_value=self.table)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    @staticmethod
    def event(method, path, user_sub="user-123", body=None):
        return {
            "version": "2.0",
            "rawPath": path,
            "requestContext": {
                "http": {"method": method},
                "authorizer": {"jwt": {"claims": {"sub": user_sub}}},
            },
            "body": json.dumps(body) if body is not None else None,
        }

    def test_requires_authenticated_user(self):
        response = lambda_handler(self.event("POST", "/sessions", user_sub=None), None)
        self.assertEqual(response["statusCode"], 401)

    def test_health_does_not_require_cognito_claims(self):
        response = lambda_handler(self.event("GET", "/health", user_sub=None), None)
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(json.loads(response["body"]), {"status": "ok"})

    def test_create_then_send_and_read_messages(self):
        created = lambda_handler(self.event("POST", "/sessions"), None)
        self.assertEqual(created["statusCode"], 201)
        session_id = json.loads(created["body"])["id"]

        sent = lambda_handler(
            self.event("POST", f"/sessions/{session_id}/messages", body={"content": "hello"}),
            None,
        )
        sent_body = json.loads(sent["body"])
        self.assertEqual(sent["statusCode"], 200)
        self.assertEqual(sent_body["type"], "answer")
        self.assertIn("not connected yet", sent_body["message"]["content"])

        read = lambda_handler(self.event("GET", f"/sessions/{session_id}"), None)
        read_body = json.loads(read["body"])
        self.assertEqual(read["statusCode"], 200)
        self.assertEqual([m["role"] for m in read_body["messages"]], ["user", "assistant"])
        self.assertEqual(read_body["messages"][0]["content"], "hello")

    def test_other_user_cannot_read_session(self):
        created = lambda_handler(self.event("POST", "/sessions"), None)
        session_id = json.loads(created["body"])["id"]
        response = lambda_handler(self.event("GET", f"/sessions/{session_id}", user_sub="other-user"), None)
        self.assertEqual(response["statusCode"], 404)

    def test_rejects_invalid_message(self):
        created = lambda_handler(self.event("POST", "/sessions"), None)
        session_id = json.loads(created["body"])["id"]
        response = lambda_handler(
            self.event("POST", f"/sessions/{session_id}/messages", body={"content": ""}),
            None,
        )
        self.assertEqual(response["statusCode"], 422)


if __name__ == "__main__":
    unittest.main()
