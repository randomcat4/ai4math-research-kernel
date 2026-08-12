from rk.cli import _parser


def test_chinese_create_command_and_flags_normalize_to_public_operation() -> None:
    args = _parser().parse_args(["--配置", "rk.json", "创建", "--凭据文件", "cap.json"])
    assert args.operation == "create"
    assert str(args.config) == "rk.json"
    assert str(args.cap_file) == "cap.json"


def test_chinese_inspect_command_and_flags_normalize_to_public_operation() -> None:
    args = _parser().parse_args(
        ["查看", "--句柄", "run-1", "--游标后", "7", "--条数", "20"]
    )
    assert args.operation == "inspect"
    assert args.handle == "run-1"
    assert args.after_cursor == 7
    assert args.limit == 20
