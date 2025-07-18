import pytest
import json
from unittest.mock import Mock, patch, MagicMock
import payment_checker

class TestPaymentChecker:
    """決済確認機能のテストクラス"""
    
    @patch('payment_checker.insert_debit_history')
    @patch('payment_checker.update_billing_to_settled')
    @patch('payment_checker.send_request_debit')
    @patch('payment_checker.parse_message')
    @patch('payment_checker.get_request_body')
    def test_lambda_handler_success(self, mock_get_body, mock_parse, mock_send, mock_update, mock_insert):
        """正常な決済処理のテスト"""
        event = {
            "Records": [{
                "body": json.dumps({
                    "billing_id": "bill_123",
                    "merchant_code": "merchant_456", 
                    "user_code": "user_789",
                    "direct_debit_id": "debit_101",
                    "amount": "1000"
                })
            }]
        }
        context = Mock()
        
        mock_parse.return_value = (
            {"test": "data"}, "bill_123", "merchant_456", "user_789", "debit_101", "1000"
        )
        mock_get_body.return_value = {"customer_id": 101, "amount": 1000}
        
        mock_response = Mock()
        mock_response.ok = True
        mock_response.json.return_value = {"status": 1, "request_id": "req_123"}
        mock_send.return_value = mock_response
        
        result = payment_checker.lambda_handler(event, context)
        
        assert result["statusCode"] == 200
        assert "successed" in result["body"]
        mock_parse.assert_called_once()
        mock_send.assert_called_once()
        mock_insert.assert_called_once()
        mock_update.assert_called_once()
    
    @patch('payment_checker.update_billing_error')
    @patch('payment_checker.send_request_debit')
    @patch('payment_checker.parse_message')
    @patch('payment_checker.get_request_body')
    def test_lambda_handler_api_error(self, mock_get_body, mock_parse, mock_send, mock_update_error):
        """API エラーのテスト"""
        event = {
            "Records": [{
                "body": json.dumps({
                    "billing_id": "bill_123",
                    "merchant_code": "merchant_456",
                    "user_code": "user_789", 
                    "direct_debit_id": "debit_101",
                    "amount": "1000"
                })
            }]
        }
        context = Mock()
        
        mock_parse.return_value = (
            {"test": "data"}, "bill_123", "merchant_456", "user_789", "debit_101", "1000"
        )
        mock_get_body.return_value = {"customer_id": 101, "amount": 1000}
        
        mock_response = Mock()
        mock_response.ok = False
        mock_response.json.return_value = {"err": {"ec": "E001"}}
        mock_response.raise_for_status.side_effect = Exception("API Error")
        mock_send.return_value = mock_response
        
        result = payment_checker.lambda_handler(event, context)
        
        assert result["statusCode"] == 500
        mock_update_error.assert_called_once_with("merchant_456", "user_789", "E001")
    
    def test_parse_message_success(self):
        """正常なメッセージパースのテスト"""
        record = {
            "body": json.dumps({
                "billing_id": "bill_123",
                "merchant_code": "merchant_456",
                "user_code": "user_789",
                "direct_debit_id": "debit_101", 
                "amount": "1000.50"
            })
        }
        
        result = payment_checker.parse_message(record)
        
        assert result[1] == "bill_123"
        assert result[2] == "merchant_456"
        assert result[3] == "user_789"
        assert result[4] == "debit_101"
        assert result[5] == "1000"
    
    def test_parse_message_missing_field(self):
        """必須フィールド不足のテスト"""
        record = {
            "body": json.dumps({
                "billing_id": "bill_123",
                "merchant_code": "merchant_456"
            })
        }
        
        with pytest.raises(ValueError, match="Missing required field"):
            payment_checker.parse_message(record)
    
    def test_parse_message_invalid_amount(self):
        """無効な金額のテスト"""
        record = {
            "body": json.dumps({
                "billing_id": "bill_123",
                "merchant_code": "merchant_456",
                "user_code": "user_789",
                "direct_debit_id": "debit_101",
                "amount": "invalid_amount"
            })
        }
        
        with pytest.raises(ValueError, match="Invalid amount"):
            payment_checker.parse_message(record)
    
    def test_parse_message_null_field(self):
        """nullフィールドのテスト"""
        record = {
            "body": json.dumps({
                "billing_id": "bill_123",
                "merchant_code": None,
                "user_code": "user_789",
                "direct_debit_id": "debit_101",
                "amount": "1000"
            })
        }
        
        with pytest.raises(ValueError, match="cannot be null"):
            payment_checker.parse_message(record)
    
    def test_get_request_body(self):
        """リクエストボディ作成のテスト"""
        result = payment_checker.get_request_body("123", "1000")
        
        expected = {
            "customer_id": 123,
            "amount": 1000,
            "tax": 0,
            "ship_fee": 0,
            "transfer_type": 1,
            "status": 1
        }
        
        assert result == expected
    
    @patch('payment_checker.requests.post')
    def test_send_request_debit_success(self, mock_post):
        """正常なAPIリクエストのテスト"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": 1}
        mock_post.return_value = mock_response
        
        body = {"customer_id": 123, "amount": 1000}
        result = payment_checker.send_request_debit(body)
        
        assert result.status_code == 200
        mock_post.assert_called_once()
    
    @patch('payment_checker.requests.post')
    def test_send_request_debit_exception(self, mock_post):
        """APIリクエスト例外のテスト"""
        mock_post.side_effect = Exception("Network error")
        
        body = {"customer_id": 123, "amount": 1000}
        
        with pytest.raises(Exception, match="Network error"):
            payment_checker.send_request_debit(body)
    
    @patch('payment_checker.get_mysql_connection')
    def test_execution_query_success(self, mock_get_conn):
        """SQLクエリ実行成功のテスト"""
        mock_conn = Mock()
        mock_cursor = Mock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn
        
        result = payment_checker.execution_query("UPDATE test SET flag=1")
        
        assert result is True
        mock_cursor.execute.assert_called_once()
        mock_conn.commit.assert_called_once()
    
    @patch('payment_checker.get_mysql_connection')
    def test_execution_query_failure(self, mock_get_conn):
        """SQLクエリ実行失敗のテスト"""
        mock_get_conn.side_effect = Exception("Database error")
        
        result = payment_checker.execution_query("UPDATE test SET flag=1")
        
        assert result is False
    
    @patch('payment_checker.parse_message')
    def test_lambda_handler_parse_error(self, mock_parse):
        """メッセージパースエラーのテスト"""
        event = {"Records": [{"body": "invalid json"}]}
        context = Mock()
        
        mock_parse.side_effect = ValueError("Invalid JSON format")
        
        result = payment_checker.lambda_handler(event, context)
        
        assert result["statusCode"] == 500
        assert "APIRequestに失敗しました" in result["body"]
    
    @patch('payment_checker.execution_query')
    def test_insert_debit_history(self, mock_exec):
        """履歴挿入のテスト"""
        mock_exec.return_value = True
        
        data = {
            "request_id": "req_123",
            "amount": 1000,
            "tax": 0,
            "ship_fee": 0,
            "custom_code": "code_123",
            "next_transfer": "2024-01-01",
            "transfer_type": 1,
            "transfer_count": 1,
            "status": 1,
            "item_code": "item_123"
        }
        
        payment_checker.insert_debit_history(data, "merchant_456", "user_789")
        
        mock_exec.assert_called_once()
    
    @patch('payment_checker.execution_query')
    def test_update_billing_to_settled(self, mock_exec):
        """請求成功更新のテスト"""
        mock_exec.return_value = True
        
        payment_checker.update_billing_to_settled("merchant_456", "user_789")
        
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args[0][0]
        assert "Billing_FLG = '2'" in call_args
        assert "Settlement_FLG = '1'" in call_args
    
    @patch('payment_checker.execution_query')
    def test_update_billing_error(self, mock_exec):
        """請求エラー更新のテスト"""
        mock_exec.return_value = True
        
        payment_checker.update_billing_error("merchant_456", "user_789", "E001")
        
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args[0][0]
        assert "Billing_FLG = '2'" in call_args
        assert "Settlement_FLG = '2'" in call_args
        assert "Error_Comment = 'E001'" in call_args