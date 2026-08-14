from types import SimpleNamespace
from unittest.mock import MagicMock

from src.services.code_browsing_service import CodeBrowsingService
from src.tools.taint_analysis_tools import _parse_taint_call_result

CODEBASE_HASH = "553642871dd4251d"


def _service(data, db_manager=None):
    codebase_tracker = MagicMock()
    codebase_tracker.get_codebase.return_value = SimpleNamespace(
        cpg_path="/tmp/test.cpg"
    )
    query_executor = MagicMock()
    query_executor.execute_query.return_value = SimpleNamespace(
        success=True,
        data=data,
    )
    if db_manager is not None:
        db_manager.get_cached_tool_output.return_value = None
    return (
        CodeBrowsingService(codebase_tracker, query_executor, db_manager),
        query_executor,
    )


def test_list_methods_uses_and_parses_named_maps():
    db_manager = MagicMock()
    service, query_executor = _service(
        [
            {
                "name": "main",
                "node_id": "42",
                "fullName": "main",
                "signature": "int main()",
                "filename": "main.c",
                "lineNumber": "10",
                "lineNumberEnd": "12",
                "cyclomaticComplexity": "3",
                "isExternal": "false",
            }
        ],
        db_manager,
    )

    result = service.list_methods(CODEBASE_HASH)

    assert result["methods"] == [
        {
            "name": "main",
            "node_id": "42",
            "fullName": "main",
            "signature": "int main()",
            "filename": "main.c",
            "lineNumber": 10,
            "lineNumberEnd": 12,
            "cyclomaticComplexity": 3,
            "numberOfLines": 3,
            "isExternal": False,
        }
    ]
    query = query_executor.execute_query.call_args.kwargs["query"]
    assert ".map(m => Map(" in query
    cache_params = db_manager.get_cached_tool_output.call_args.args[2]
    assert cache_params["result_format"] == "named-map-v1"


def test_list_calls_uses_and_parses_named_maps():
    service, query_executor = _service(
        [
            {
                "caller": "main",
                "callee": "add",
                "code": "add(2, 3)",
                "filename": "main.c",
                "lineNumber": "8",
            }
        ]
    )

    result = service.list_calls(CODEBASE_HASH)

    assert result["calls"][0]["lineNumber"] == 8
    assert result["calls"][0]["callee"] == "add"
    assert ".map(c => Map(" in query_executor.execute_query.call_args.kwargs["query"]


def test_list_parameters_uses_and_parses_nested_named_maps():
    service, query_executor = _service(
        [
            {
                "method": "add",
                "parameters": [
                    {"name": "a", "type": "int", "index": "1"},
                    {"name": "b", "type": "int", "index": "2"},
                ],
            }
        ]
    )

    result = service.list_parameters(CODEBASE_HASH)

    assert result["methods"][0] == {
        "method": "add",
        "parameters": [
            {"name": "a", "type": "int", "index": 1},
            {"name": "b", "type": "int", "index": 2},
        ],
    }
    assert ".map(m => Map(" in query_executor.execute_query.call_args.kwargs["query"]


def test_find_literals_uses_and_parses_named_maps():
    service, query_executor = _service(
        [
            {
                "value": '"hello"',
                "type": "char *",
                "filename": "main.c",
                "lineNumber": "7",
                "method": "main",
            }
        ]
    )

    result = service.find_literals(CODEBASE_HASH)

    assert result["literals"][0]["lineNumber"] == 7
    assert result["literals"][0]["value"] == '"hello"'
    assert ".map(lit => Map(" in query_executor.execute_query.call_args.kwargs["query"]


def test_taint_call_parser_accepts_named_map_scalars():
    result = _parse_taint_call_result(
        {
            "node_id": "123",
            "name": "getenv",
            "code": 'getenv("HOME")',
            "filename": "main.c",
            "lineNumber": "9",
            "method": "main",
        }
    )

    assert result == {
        "node_id": "123",
        "name": "getenv",
        "code": 'getenv("HOME")',
        "filename": "main.c",
        "lineNumber": 9,
        "method": "main",
    }
