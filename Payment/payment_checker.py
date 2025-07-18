import os
import json
import pymysql
import logging
import boto3
import time
from typing import Dict, Optional
import requests

# 環境変数
API_KEY = os.environ.get("API_KEY")
DB_HOST = os.environ["DB_ENDPOINT"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]
DB_NAME = os.environ["DB_NAME"]
DEBIT_ENDPOINT = os.environ["DEBIT_ENDPOINT"]

# ログ設定
ssm_client = boto3.client('ssm')
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

# エラーレスポンス定数
ERROR_CODE_DB = {
    'statusCode': 500,
    'body': json.dumps("error: There is duplicate data in tbl_billinguser")
}
ERROR_CODE_REQUEST = {
    'statusCode': 500,
    'body': json.dumps("error: APIRequestに失敗しました")
}
ERROR_CODE_SOMETHING = {
    'statusCode': 500,
    'body': json.dumps("error: There is something wrong")
}

# ステータス定数
SUCCESS_FLAG = 1
FAULT_FLAG = 2
BILLING_FLAG_PENDING = '1'
BILLING_FLAG_FINISHED = '2'
SETTLEMENT_SUCCESS = '1'
SETTLEMENT_FAILURE = '2'

# グローバル変数
_mysql_connection: Optional[pymysql.connections.Connection] = None

def lambda_handler(event, context):
    """Lambda のメインハンドラー関数"""
    records = event.get("Records", [])
    success_count = 0
    assert len(records) == 1
    record = records[0]
    logger.info(f"Received {record}.")
    
    try:
        message_data, billing_id, merchant_code, user_code, direct_debit_id, amount = parse_message(record)
        body = get_request_body(direct_debit_id, amount)
        response = send_request_debit(body)
        response_json = response.json()
        
        if not response.ok:
            ec = response_json['err']['ec']
            update_billing_error(merchant_code, user_code, ec)
            response.raise_for_status()
        
        insert_debit_history(response_json, merchant_code, user_code)
        
        if response_json['status'] == SUCCESS_FLAG:
            update_billing_to_settled(merchant_code, user_code)
        else:
            update_billing_error(merchant_code, user_code, "error")
            raise ValueError('status is disabled')
        
        time.sleep(1)
        success_count += 1
        
    except Exception as e:
        logger.error(f"Error processing record: {e}")
        return ERROR_CODE_REQUEST
    
    return {
        "statusCode": 200,
        "body": json.dumps(f'successed success_count / num_records = {success_count} / {len(records)}')
    }

def parse_message(record: dict) -> tuple:
    """SQS レコードの body をパースし、必要なフィールドが全て存在するかを検証する"""
    body_str = record.get("body", "")
    logger.info(f"Raw message body: {body_str}")
    try:
        message_data = json.loads(body_str)
    except json.JSONDecodeError as e:
        logger.exception("JSON のパースに失敗しました。")
        raise ValueError("Invalid JSON format") from e
    
    required_fields = ["billing_id", "merchant_code", "user_code", "direct_debit_id", "amount"]
    for field in required_fields:
        if field not in message_data:
            logger.warning(f"フィールド '{field}' がありません: {message_data}")
            raise ValueError(f"Missing required field: {field}")
        if message_data[field] is None:
            raise ValueError(f"Field '{field}' cannot be null")
    
    raw_amount = message_data["amount"]
    try:
        amount = str(int(float(raw_amount)))
    except ValueError as e:
        logger.exception(f"金額(amount)の変換に失敗しました: {raw_amount}")
        raise ValueError(f"Invalid amount: {raw_amount}") from e
    
    return (
        message_data,
        message_data["billing_id"],
        message_data["merchant_code"],
        message_data["user_code"],
        message_data["direct_debit_id"],
        amount
    )

def get_request_body(direct_debit_id: str, total_amount: str) -> dict:
    """ロボペイに送信するリクエストボディを作成"""
    body = {
        "customer_id": int(direct_debit_id),
        "amount": int(total_amount),
        "tax": 0,
        "ship_fee": 0,
        "transfer_type": 1,
        "status": 1
    }
    return body

def send_request_debit(body: dict) -> requests.Response:
    """指定されたボディを JSON 化して DEBIT_ENDPOINT にPOSTリクエストを送信"""
    headers = {
        'X-Payment-API-Key': API_KEY,
        'Content-Type': 'application/json'
    }
    try:
        logger.debug(f"パラメータ: {body}")
        encoded_param = json.dumps(body).encode("utf-8")
        response = requests.post(DEBIT_ENDPOINT, data=encoded_param, headers=headers)
        logger.info(f"response: {response}")
        return response
    except Exception as e:
        logger.error(f"予期しないエラーが発生しました: {e}")
        raise e

def insert_debit_history(data: dict, merchant_code: str, user_code: str):
    """TBL_DEBITHISTORY テーブルへデータを挿入する"""
    sql = f"""
    INSERT INTO TBL_DEBITHISTORY (
        request_id, Merchant_Code, User_Code, amount, tax, ship_fee,
        custom_code, next_transfer, transfer_type, transfer_count, status, item_code
    ) VALUES (
        '{data.get('request_id')}',
        '{merchant_code}',
        '{user_code}',
        {data.get('amount')},
        {data.get('tax')},
        {data.get('ship_fee')},
        '{data.get('custom_code')}',
        '{data.get('next_transfer')}',
        '{data.get('transfer_type')}',
        {data.get('transfer_count')},
        '{data.get('status')}',
        '{data.get('item_code')}'
    );
    """
    try:
        execution_query(sql)
        log_id = data.get('request_id', 'N/A')
        logger.info(f"Data inserted into TBL_DEBITHISTORY: {log_id}")
    except Exception as e:
        logger.error(f"Failed to insert data into TBL_DEBITHISTORY: {str(e)}")
        raise e

def update_billing_to_settled(merchant_code: str, user_code: str):
    """支払い成功時に、対象レコードの請求フラグを「成功」に更新する"""
    query = f"""
    UPDATE TBL_USER_PAYMENT
    SET `Billing_FLG` = '{BILLING_FLAG_FINISHED}', `Settlement_FLG` = '{SETTLEMENT_SUCCESS}'
    WHERE `Merchant_Code` = '{merchant_code}'
      AND `User_Code` = '{user_code}'
      AND `Billing_FLG` = '{BILLING_FLAG_PENDING}'
    """.strip()
    execution_query(query)

def update_billing_error(merchant_code: str, user_code: str, error: str):
    """支払い失敗時に、対象レコードの請求フラグとエラー内容を更新する"""
    query = f"""
    UPDATE TBL_USER_PAYMENT
    SET `Billing_FLG` = '{BILLING_FLAG_FINISHED}', `Settlement_FLG` = '{SETTLEMENT_FAILURE}', `Error_Comment` = '{error}'
    WHERE `Merchant_Code` = '{merchant_code}'
      AND `User_Code` = '{user_code}'
      AND `Billing_FLG` = '{BILLING_FLAG_PENDING}'
    """.strip()
    execution_query(query)

def execution_query(query: str) -> bool:
    """SQLクエリを実行するメソッド"""
    try:
        connection = get_mysql_connection()
        with connection.cursor() as cursor:
            cursor.execute(query)
            connection.commit()
        logger.info('Updated')
        return True
    except Exception as e:
        logger.error(f"Error occurred while updating billingFlg: {str(e)}")
        return False

def get_mysql_connection() -> pymysql.connections.Connection:
    """グローバルな MySQL 接続オブジェクトを取得する"""
    global _mysql_connection
    if _mysql_connection is not None and _mysql_connection.open:
        return _mysql_connection
    _mysql_connection = pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        passwd=DB_PASSWORD,
        db=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor
    )
    return _mysql_connection