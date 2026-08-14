import logging
from typing import Any, Dict, Optional
from ..exceptions import ValidationError
from ..utils.validators import validate_codebase_hash
from ..utils.query_rendering import escape_scala_string

logger = logging.getLogger(__name__)

_RESULT_FORMAT_VERSION = "named-map-v1"


def _coerce_int(value: Any, default: int = -1) -> int:
    """Convert Joern JSON scalar values to integers without leaking parse errors."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Convert Joern JSON scalar values to booleans."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "false"}:
            return normalized == "true"
    if value is None:
        return default
    return bool(value)


class CodeBrowsingService:
    """Service for code browsing operations with caching support"""

    def __init__(self, codebase_tracker, query_executor, db_manager=None):
        self.codebase_tracker = codebase_tracker
        self.query_executor = query_executor
        self.db_manager = db_manager

    def _get_cached_or_execute(self, tool_name: str, codebase_hash: str, params: Dict[str, Any], query_func):
        """Helper to check cache, execute query if needed, and cache result"""
        if self.db_manager:
            cached = self.db_manager.get_cached_tool_output(tool_name, codebase_hash, params)
            if cached is not None:
                return cached

        result = query_func()
        
        if self.db_manager and result:
             # Only cache successful results that are not error dicts
             if isinstance(result, dict) and result.get("success", False):
                 self.db_manager.cache_tool_output(tool_name, codebase_hash, params, result)
        
        return result

    def list_methods(
        self,
        codebase_hash: str,
        name_pattern: Optional[str] = None,
        file_pattern: Optional[str] = None,
        callee_pattern: Optional[str] = None,
        include_external: bool = False,
        limit: int = 1000,
        page: int = 1,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        
        validate_codebase_hash(codebase_hash)
        
        # Cache key parameters (excluding pagination)
        cache_params = {
            "name_pattern": name_pattern,
            "file_pattern": file_pattern,
            "callee_pattern": callee_pattern,
            "include_external": include_external,
            "limit": limit,
            "result_format": _RESULT_FORMAT_VERSION,
        }

        def execute_query():
            codebase_info = self.codebase_tracker.get_codebase(codebase_hash)
            if not codebase_info:
                raise ValidationError(f"Codebase not found for codebase {codebase_hash}")

            query_parts = ["cpg.method"]
            if not include_external:
                query_parts.append(".isExternal(false)")
            if name_pattern:
                query_parts.append(f'.name("{escape_scala_string(name_pattern)}")')
            if file_pattern:
                query_parts.append(f'.where(_.file.name("{escape_scala_string(file_pattern)}"))')
            if callee_pattern:
                query_parts.append(f'.where(_.callOut.name("{escape_scala_string(callee_pattern)}"))')

            query_parts.append(
                '.map(m => Map("name" -> m.name, "node_id" -> m.id.toString, '
                '"fullName" -> m.fullName, "signature" -> m.signature, '
                '"filename" -> m.filename, "lineNumber" -> m.lineNumber.getOrElse(-1).toString, '
                '"lineNumberEnd" -> m.lineNumberEnd.getOrElse(-1).toString, '
                '"cyclomaticComplexity" -> (m.controlStructure.size + 1).toString, '
                '"isExternal" -> m.isExternal.toString))'
            )
            
            query_limit = min(limit, 10000)
            query = "".join(query_parts) + f".dedup.take({query_limit}).l"
            
            result = self.query_executor.execute_query(
                codebase_hash=codebase_hash,
                cpg_path=codebase_info.cpg_path,
                query=query,
                timeout=30,
                limit=query_limit,
            )

            if not result.success:
                return {"success": False, "error": {"code": "QUERY_ERROR", "message": result.error}}

            methods = []
            for item in result.data:
                if isinstance(item, dict):
                    line_number = _coerce_int(item.get("lineNumber", item.get("_6")), -1)
                    line_number_end = _coerce_int(item.get("lineNumberEnd", item.get("_7")), -1)
                    
                    # Calculate number of lines
                    if line_number != -1 and line_number_end != -1:
                        number_of_lines = line_number_end - line_number + 1
                    else:
                        number_of_lines = 0

                    methods.append({
                        "name": item.get("name", item.get("_1", "")),
                        "node_id": str(item.get("node_id", item.get("_2", ""))),
                        "fullName": item.get("fullName", item.get("_3", "")),
                        "signature": item.get("signature", item.get("_4", "")),
                        "filename": item.get("filename", item.get("_5", "")),
                        "lineNumber": line_number,
                        "lineNumberEnd": line_number_end,
                        "cyclomaticComplexity": _coerce_int(
                            item.get("cyclomaticComplexity", item.get("_8")), 1
                        ),
                        "numberOfLines": number_of_lines,
                        "isExternal": _coerce_bool(
                            item.get("isExternal", item.get("_9")), False
                        ),
                    })
            return {"success": True, "methods": methods, "total": len(methods)}

        # Get full result (cached or fresh)
        full_result = self._get_cached_or_execute("list_methods", codebase_hash, cache_params, execute_query)
        
        if not full_result.get("success"):
            return full_result

        methods = full_result.get("methods", [])
        # Respect the provided 'limit' for the returned list, independent of page_size
        if limit is not None and limit > 0:
            methods = methods[:limit]
        total = len(methods)
        
        # Pagination
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paged_methods = methods[start_idx:end_idx]

        return {
            "success": True,
            "methods": paged_methods,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size if page_size > 0 else 1
        }


    def list_calls(
        self,
        codebase_hash: str,
        caller_pattern: Optional[str] = None,
        callee_pattern: Optional[str] = None,
        limit: int = 1000,
        page: int = 1,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        
        validate_codebase_hash(codebase_hash)
        cache_params = {
            "caller_pattern": caller_pattern,
            "callee_pattern": callee_pattern,
            "limit": limit,
            "result_format": _RESULT_FORMAT_VERSION,
        }

        def execute_query():
            codebase_info = self.codebase_tracker.get_codebase(codebase_hash)
            if not codebase_info or not codebase_info.cpg_path:
                raise ValidationError(f"CPG not found for codebase {codebase_hash}")

            query_parts = ["cpg.call"]
            if callee_pattern:
                query_parts.append(f'.name("{escape_scala_string(callee_pattern)}")')
            if caller_pattern:
                query_parts.append(f'.where(_.method.name("{escape_scala_string(caller_pattern)}"))')

            query_parts.append(
                '.map(c => Map("caller" -> c.method.name, "callee" -> c.name, '
                '"code" -> c.code, "filename" -> c.method.filename, '
                '"lineNumber" -> c.lineNumber.getOrElse(-1).toString))'
            )
            
            query_limit = min(limit, 10000)
            query = "".join(query_parts) + f".dedup.take({query_limit}).l"
            
            result = self.query_executor.execute_query(
                codebase_hash=codebase_hash,
                cpg_path=codebase_info.cpg_path,
                query=query,
                timeout=30,
                limit=query_limit,
            )

            if not result.success:
                return {"success": False, "error": {"code": "QUERY_ERROR", "message": result.error}}

            calls = []
            for item in result.data:
                if isinstance(item, dict):
                    calls.append({
                        "caller": item.get("caller", item.get("_1", "")),
                        "callee": item.get("callee", item.get("_2", "")),
                        "code": item.get("code", item.get("_3", "")),
                        "filename": item.get("filename", item.get("_4", "")),
                        "lineNumber": _coerce_int(
                            item.get("lineNumber", item.get("_5")), -1
                        ),
                    })
            return {"success": True, "calls": calls, "total": len(calls)}

        full_result = self._get_cached_or_execute("list_calls", codebase_hash, cache_params, execute_query)
        
        if not full_result.get("success"):
            return full_result

        calls = full_result.get("calls", [])
        # Apply the provided limit to final result set
        if limit is not None and limit > 0:
            calls = calls[:limit]
        total = len(calls)
        
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paged_calls = calls[start_idx:end_idx]

        return {
            "success": True,
            "calls": paged_calls,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size if page_size > 0 else 1
        }

    def list_parameters(
        self,
        codebase_hash: str,
        method_name: Optional[str] = None,
        limit: int = 1000,
    ) -> Dict[str, Any]:
        
        validate_codebase_hash(codebase_hash)
        cache_params = {
            "method_name": method_name,
            "limit": limit,
            "result_format": _RESULT_FORMAT_VERSION,
        }

        def execute_query():
            codebase_info = self.codebase_tracker.get_codebase(codebase_hash)
            if not codebase_info or not codebase_info.cpg_path:
                raise ValidationError(f"CPG not found for codebase {codebase_hash}")

            query_parts = ["cpg.method"]
            if method_name:
                query_parts.append(f'.name("{escape_scala_string(method_name)}")')
            
            query_parts.append(
                '.map(m => Map("method" -> m.name, "parameters" -> '
                'm.parameter.map(p => Map("name" -> p.name, "type" -> p.typeFullName, '
                '"index" -> p.index.toString)).l))'
            )
            
            query = "".join(query_parts) + f".take({limit}).l"
            
            result = self.query_executor.execute_query(
                codebase_hash=codebase_hash,
                cpg_path=codebase_info.cpg_path,
                query=query,
                timeout=30,
                limit=limit,
            )

            if not result.success:
                return {"success": False, "error": {"code": "QUERY_ERROR", "message": result.error}}

            methods = []
            for item in result.data:
                if isinstance(item, dict) and (
                    ("method" in item and "parameters" in item)
                    or ("_1" in item and "_2" in item)
                ):
                    params = []
                    param_list = item.get("parameters", item.get("_2", []))
                    for param_data in param_list:
                        if isinstance(param_data, dict):
                            params.append({
                                "name": param_data.get("name", param_data.get("_1", "")),
                                "type": param_data.get("type", param_data.get("_2", "")),
                                "index": _coerce_int(
                                    param_data.get("index", param_data.get("_3")), -1
                                ),
                            })
                    methods.append({
                        "method": item.get("method", item.get("_1", "")),
                        "parameters": params,
                    })
            return {"success": True, "methods": methods, "total": len(methods)}

        return self._get_cached_or_execute("list_parameters", codebase_hash, cache_params, execute_query)

    def find_literals(
        self,
        codebase_hash: str,
        pattern: Optional[str] = None,
        literal_type: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        
        validate_codebase_hash(codebase_hash)
        cache_params = {
            "pattern": pattern,
            "literal_type": literal_type,
            "limit": limit,
            "result_format": _RESULT_FORMAT_VERSION,
        }

        def execute_query():
            codebase_info = self.codebase_tracker.get_codebase(codebase_hash)
            if not codebase_info or not codebase_info.cpg_path:
                raise ValidationError(f"CPG not found for codebase {codebase_hash}")

            query_parts = ["cpg.literal"]
            if pattern:
                query_parts.append(f'.code("{escape_scala_string(pattern)}")')
            if literal_type:
                query_parts.append(f'.typeFullName(".*{escape_scala_string(literal_type)}.*")')

            query_parts.append(
                '.map(lit => Map("value" -> lit.code, "type" -> lit.typeFullName, '
                '"filename" -> lit.filename, '
                '"lineNumber" -> lit.lineNumber.getOrElse(-1).toString, '
                '"method" -> lit.method.name))'
            )
            
            query = "".join(query_parts) + f".take({limit}).l"
            
            result = self.query_executor.execute_query(
                codebase_hash=codebase_hash,
                cpg_path=codebase_info.cpg_path,
                query=query,
                timeout=30,
                limit=limit,
            )

            if not result.success:
                return {"success": False, "error": {"code": "QUERY_ERROR", "message": result.error}}

            literals = []
            for item in result.data:
                if isinstance(item, dict):
                    literals.append({
                        "value": item.get("value", item.get("_1", "")),
                        "type": item.get("type", item.get("_2", "")),
                        "filename": item.get("filename", item.get("_3", "")),
                        "lineNumber": _coerce_int(
                            item.get("lineNumber", item.get("_4")), -1
                        ),
                        "method": item.get("method", item.get("_5", "")),
                    })
            return {"success": True, "literals": literals, "total": len(literals)}

        return self._get_cached_or_execute("find_literals", codebase_hash, cache_params, execute_query)

    def warm_up_cache(self, codebase_hash: str):
        """Run default queries to warm the cache.

        These all target the same CPG, so they serialize on the per-codebase
        query lock regardless — the old ThreadPoolExecutor was false parallelism
        (5 threads idling on the lock). Run them sequentially; a failing query is
        logged and skipped so it doesn't abort the rest. Callers run this off the
        build-worker critical path (see core_tools._schedule_warmup).
        """
        logger.info(f"Warming up cache for codebase {codebase_hash}")
        tasks = [
            self.list_methods,
            self.list_calls,
            self.list_parameters,
            self.find_literals,
        ]
        for func in tasks:
            try:
                func(codebase_hash)
                logger.info(f"Cache warm-up task {func.__name__} completed for {codebase_hash}")
            except Exception as e:
                logger.error(f"Cache warm-up task {func.__name__} failed for {codebase_hash}: {e}")
        logger.info(f"Cache warm-up complete for {codebase_hash}")
