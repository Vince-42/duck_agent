import sys
import json
from knit import Compiler

if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit(2)
    
    filepath = sys.argv[1]
    comp = Compiler()
    try:
        rc = comp.run(filepath)
        sys.exit(rc)
    except Exception as e:
        print(json.dumps({
            "pattern_name": None, "cast_on": None, "valid": False,
            "errors": [{"type": "error", "code": "FILE_NOT_FOUND", "message": str(e), "line": None, "row": None}],
            "expanded_rows": [], "final_stitch_count": None, "bind_off": False,
        }))
        sys.exit(1)
