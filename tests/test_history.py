import pytest

from fe_cli.history import recent_history

HEADER = "       JobID    JobName      State ExitCode AllocCPUS ReqMem Elapsed NodeList"
SEPARATOR = "------------ ---------- ---------- -------- --------- ------ -------- --------"


def job(job_id: str | int, name: str = "factor") -> str:
    return f"{job_id:>12} {name} COMPLETED 0:0 6 18G 00:01:00 quant-a"


def table(*rows: str, final_newline: bool = True) -> str:
    return "\n".join([HEADER, SEPARATOR, *rows]) + ("\n" if final_newline else "")


def test_latest_ten_records_preserve_header_and_separator():
    source = table(*(job(job_id) for job_id in range(1, 16)))

    result = recent_history(source)

    assert result == table(*(job(job_id) for job_id in range(15, 5, -1)))
    assert len(result.splitlines()) == 12
    assert result.count(HEADER) == result.count(SEPARATOR) == 1


def test_numeric_sorting_and_custom_limit():
    assert recent_history(table(job(10), job(100), job(9)), 2) == table(job(100), job(10))


def test_arrays_steps_and_stable_order_for_equal_primary_ids():
    source = table(job("9.batch"), job("100_3"), job("10.extern"), job("100_3.batch"))
    assert recent_history(source) == table(
        job("100_3"), job("100_3.batch"), job("10.extern"), job("9.batch")
    )


@pytest.mark.parametrize("output", ["", "\n \n", "No history found.\n", "SIMULATED\njob 1\n"])
def test_empty_and_short_unknown_output_are_unchanged(output):
    assert recent_history(output) == output


@pytest.mark.parametrize("output", [HEADER, HEADER + "\n", table(), "\n" + table() + "\n"])
def test_header_only_output_is_unchanged(output):
    assert recent_history(output) == output


def test_blank_lines_and_missing_final_newline():
    source = "\n" + table(job(9), "", job(10), final_newline=False)
    assert recent_history(source) == "\n" + table(job(10), "", job(9), final_newline=False)


def test_markup_unicode_and_original_spacing_remain_literal():
    row = job(100, "[bold]因子[/bold]") + "   "
    assert recent_history(table(job(9), row)) == table(row, job(9))


def test_job_id_raw_header_is_supported():
    source = table(job(9), job(100)).replace("JobID", "JobIDRaw", 1)
    assert recent_history(source) == table(job(100), job(9)).replace("JobID", "JobIDRaw", 1)


def test_long_unrecognized_output_requires_full_history():
    with pytest.raises(ValueError, match="use history full"):
        recent_history("\n".join(f"unknown {index}" for index in range(11)))


@pytest.mark.parametrize("unexpected", ["warning: data unavailable", "123oops job", HEADER])
def test_unexpected_content_after_recognized_header_requires_full_history(unexpected):
    with pytest.raises(ValueError, match="use history full"):
        recent_history(table(job(1), unexpected))


def test_source_remains_available_for_full_history():
    source = table(*(job(job_id) for job_id in range(15)))
    original = source
    assert recent_history(source) != source
    assert source == original


@pytest.mark.parametrize("limit", [0, -1])
def test_limit_must_be_positive(limit):
    with pytest.raises(ValueError, match="positive"):
        recent_history(table(job(1)), limit)
