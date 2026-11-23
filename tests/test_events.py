"""Events are log lines with syslog levels; tgmux knows nothing about where they go."""
import logging

from tgmux import events
from tgmux.events import NOTICE, EventLog, SyslogPrefixFormatter, level_for


def test_levels_follow_syslog_meaning():
    assert level_for("lifecycle") == NOTICE
    assert level_for("registered:barabot") == NOTICE
    assert level_for("online:barabot") == NOTICE
    assert level_for("offline:barabot") == logging.WARNING
    assert level_for("unowned:-1005") == logging.WARNING
    assert level_for("call-error:barabot:ValueError") == logging.ERROR
    assert level_for("upstream-lost") == logging.ERROR


def test_repeats_are_folded_per_window(caplog):
    now = [0.0]
    log = EventLog(window=60, clock=lambda: now[0])
    with caplog.at_level(logging.INFO, logger="tgmux.events"):
        for _ in range(3):
            log.report("unowned:-1005", "сообщение в ничьём чате")
        now[0] = 61
        log.report("unowned:-1005", "сообщение в ничьём чате")
        log.report("lifecycle", "шлюз запущен", window=0)
        log.report("lifecycle", "шлюз запущен", window=0)
    lines = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert lines == [
        (logging.WARNING, "сообщение в ничьём чате"),
        (logging.WARNING, "сообщение в ничьём чате (+2 повторов за прошлый период)"),
        (NOTICE, "шлюз запущен"),
        (NOTICE, "шлюз запущен"),
    ]


def test_journald_priority_prefix():
    fmt = SyslogPrefixFormatter("%(message)s")

    def line(level, msg="x"):
        return fmt.format(logging.LogRecord("t", level, __file__, 1, msg, None, None))

    assert line(logging.ERROR) == "<3>x"
    assert line(logging.WARNING) == "<4>x"
    assert line(NOTICE) == "<5>x"
    assert line(logging.INFO) == "<6>x"
    assert line(logging.DEBUG) == "<7>x"
    assert line(logging.ERROR, "a\nb") == "<3>a\n<3>b"  # every journald line keeps the priority


def test_setup_logging_uses_prefixes_only_under_journald(monkeypatch):
    monkeypatch.setenv("JOURNAL_STREAM", "8:123")
    events.setup_logging("default")
    assert isinstance(logging.getLogger().handlers[0].formatter, SyslogPrefixFormatter)
    monkeypatch.delenv("JOURNAL_STREAM")
    events.setup_logging("default")
    assert not isinstance(logging.getLogger().handlers[0].formatter, SyslogPrefixFormatter)
