from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from hypothesis import given, settings
from hypothesis import strategies as st

from pester.scheduler.policy import (
    Candidate,
    Policy,
    QuietWindow,
    RecipientState,
    decide,
    earliest_send,
    seeded_jitter,
)

NY = ZoneInfo("America/New_York")
UTC_TZ = ZoneInfo("UTC")
NOW = datetime(2026, 9, 25, 16, tzinfo=UTC)  # 12:00 in New York
H = timedelta(hours=1)
M = timedelta(minutes=1)
NIGHT = QuietWindow(time(21, 0), time(8, 30))


def cand(key: int, recipient: str = "kate", priority: float = 0.5, age: int = 0, **kw: datetime) -> Candidate:
    return Candidate(key=key, recipient_id=recipient, priority=priority, created_at=NOW - age * H, **kw)


def kate(**kw: object) -> dict[str, RecipientState]:
    return {"kate": RecipientState(tz=NY, **kw)}  # type: ignore[arg-type]


def ny(hour: int, minute: int = 0, day: int = 25, month: int = 9) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=NY).astimezone(UTC)


# ---- Ordering and outstanding slots ------------------------------------------------------------------


def test_nothing_to_do() -> None:
    plan = decide(NOW, [], {}, Policy())
    assert plan.send == [] and plan.expire == [] and plan.wake_at is None


def test_highest_priority_then_oldest_then_key() -> None:
    jobs = [cand(1, priority=0.5, age=5), cand(2, priority=0.9), cand(3, priority=0.5, age=9), cand(4, age=9)]
    assert decide(NOW, jobs, kate(), Policy()).send == [2]
    assert decide(NOW, jobs, kate(), Policy(max_outstanding=4)).send == [2, 3, 4, 1]


def test_outstanding_limits_per_recipient() -> None:
    recipients = {"kate": RecipientState(tz=NY, outstanding=1), "sam": RecipientState(tz=UTC_TZ)}
    jobs = [cand(1, "kate"), cand(2, "kate"), cand(3, "sam")]
    assert decide(NOW, jobs, recipients, Policy()).send == [3]
    assert sorted(decide(NOW, jobs, recipients, Policy(max_outstanding=2)).send) == [1, 3]


def test_unknown_recipient_is_skipped() -> None:
    assert decide(NOW, [cand(1, "ghost")], kate(), Policy()).send == []


def test_paused_recipient_gets_nothing_but_jobs_still_expire() -> None:
    plan = decide(NOW, [cand(1), cand(2, expires_at=NOW)], kate(paused=True), Policy())
    assert plan.send == [] and plan.expire == [2]


# ---- Job timing -------------------------------------------------------------------------------------


def test_not_before_and_snooze_wait_and_set_wake() -> None:
    jobs = [cand(1, not_before=NOW + 2 * H), cand(2, snoozed_until=NOW + H)]
    plan = decide(NOW, jobs, kate(), Policy())
    assert plan.send == [] and plan.wake_at == NOW + H
    assert decide(NOW + H, jobs, kate(), Policy()).send == [2]


def test_expired_jobs_expire_even_when_blocked() -> None:
    plan = decide(NOW, [cand(1, expires_at=NOW), cand(2, expires_at=NOW - H)], kate(outstanding=1), Policy())
    assert sorted(plan.expire) == [1, 2] and plan.send == []


# ---- Quiet hours ------------------------------------------------------------------------------------


def test_quiet_window_contains() -> None:
    assert NIGHT.contains(time(23, 0)) and NIGHT.contains(time(3, 0)) and NIGHT.contains(time(21, 0))
    assert not NIGHT.contains(time(8, 30)) and not NIGHT.contains(time(12, 0))
    lunch = QuietWindow(time(12, 0), time(13, 0))
    assert lunch.contains(time(12, 30)) and not lunch.contains(time(13, 0)) and not lunch.contains(time(3, 0))
    assert not QuietWindow(time(9, 0), time(9, 0)).contains(time(9, 0))


def test_quiet_hours_spanning_midnight_defer_to_morning() -> None:
    late = ny(23)
    plan = decide(late, [cand(1)], kate(quiet=NIGHT), Policy())
    assert plan.send == []
    assert plan.wake_at == ny(8, 30, day=26)
    assert decide(ny(8, 30, day=26), [cand(1)], kate(quiet=NIGHT), Policy()).send == [1]


def test_quiet_hours_use_recipient_timezone() -> None:
    # 23:00 UTC is 19:00 in New York: fine for Kate, quiet for a UTC recipient with the same window.
    at = datetime(2026, 9, 25, 23, tzinfo=UTC)
    recipients = {"kate": RecipientState(tz=NY, quiet=NIGHT), "sam": RecipientState(tz=UTC_TZ, quiet=NIGHT)}
    assert decide(at, [cand(1, "kate"), cand(2, "sam")], recipients, Policy(max_outstanding=2)).send == [1]


def test_quiet_end_across_spring_forward() -> None:
    # 2026-03-08: New York clocks jump 02:00 -> 03:00. A window ending at 02:30 (a wall time that doesn't
    # exist that night) must still end, and never let a send happen inside the window.
    window = QuietWindow(time(1, 0), time(2, 30))
    at = datetime(2026, 3, 8, 1, 15, tzinfo=NY).astimezone(UTC)
    job = Candidate(key=1, recipient_id="kate", priority=0.5, created_at=at)
    t = earliest_send(at, job, RecipientState(tz=NY, quiet=window), Policy())
    assert t > at
    assert not window.contains(t.astimezone(NY).time())
    assert t - at < 2 * H


def test_quiet_end_across_fall_back() -> None:
    # 2026-11-01: New York repeats 01:00-02:00. A 00:00-01:30 window ends once, at the first 01:30.
    window = QuietWindow(time(0, 0), time(1, 30))
    at = datetime(2026, 11, 1, 0, 10, tzinfo=NY).astimezone(UTC)
    job = Candidate(key=1, recipient_id="kate", priority=0.5, created_at=at)
    t = earliest_send(at, job, RecipientState(tz=NY, quiet=window), Policy())
    assert t.astimezone(NY).time() == time(1, 30)
    assert t - at == timedelta(hours=1, minutes=20)


# ---- Spacing and daily caps ------------------------------------------------------------------------


def test_min_interval_since_last_prompt() -> None:
    policy = Policy(min_interval=2 * H)
    state = kate(recent_prompts=(NOW - H,))
    plan = decide(NOW, [cand(1)], state, policy)
    assert plan.send == [] and plan.wake_at == NOW + H
    assert decide(NOW + H, [cand(1)], state, policy).send == [1]


def test_min_interval_applies_within_one_pass() -> None:
    plan = decide(NOW, [cand(1), cand(2)], kate(), Policy(max_outstanding=5, min_interval=H))
    assert plan.send == [1]  # the second would be 0 minutes after the first
    assert plan.wake_at == NOW + H


def test_daily_cap_counts_the_recipients_local_day() -> None:
    policy = Policy(max_per_day=2)
    # Two prompts earlier today in New York (one of them on the previous UTC day).
    state = kate(recent_prompts=(ny(0, 30), ny(9)))
    plan = decide(ny(12), [cand(1)], state, policy)
    assert plan.send == [] and plan.wake_at == ny(0, day=26)
    assert decide(ny(0, day=26), [cand(1)], state, policy).send == [1]


def test_daily_cap_applies_within_one_pass() -> None:
    plan = decide(NOW, [cand(1), cand(2), cand(3)], kate(), Policy(max_outstanding=5, max_per_day=2))
    assert plan.send == [1, 2]


def test_daily_cap_rollover_lands_in_quiet_hours_then_waits_for_morning() -> None:
    state = kate(quiet=NIGHT, recent_prompts=(ny(10),))
    plan = decide(ny(12), [cand(1)], state, Policy(max_per_day=1))
    assert plan.wake_at == ny(8, 30, day=26)


# ---- Jitter -----------------------------------------------------------------------------------------


def test_seeded_jitter_is_deterministic_and_bounded() -> None:
    jitter = seeded_jitter("seed", 30)
    offsets = [jitter(f"k{i}") for i in range(200)]
    assert offsets == [seeded_jitter("seed", 30)(f"k{i}") for i in range(200)]
    assert all(timedelta(0) <= o <= timedelta(minutes=30) for o in offsets)
    assert len(set(offsets)) > 100
    assert offsets != [seeded_jitter("other", 30)(f"k{i}") for i in range(200)]
    assert seeded_jitter("seed", 0)("k") == timedelta(0)


def test_jittered_send_time_is_stable_across_passes() -> None:
    policy = Policy(jitter=seeded_jitter("s", 30))
    job, state = cand(1), RecipientState(tz=NY)
    first = earliest_send(NOW, job, state, policy)
    assert NOW <= first <= NOW + 30 * M
    for step in range(0, 30, 5):  # re-planning later must not keep pushing it back
        assert earliest_send(NOW + step * M, job, state, policy) == max(first, NOW + step * M)


def test_quiet_end_jitter_is_stable_after_the_nominal_end() -> None:
    # Planning from inside the window and planning just after its nominal end must agree.
    policy = Policy(jitter=seeded_jitter("s", 30))
    state = RecipientState(tz=NY, quiet=NIGHT)
    job = Candidate(key=1, recipient_id="kate", priority=0.5, created_at=ny(8, 0, day=26))
    planned = earliest_send(ny(8, 0, day=26), job, state, policy)
    assert planned > ny(8, 30, day=26)  # this seed/day gives a non-zero offset
    for minute in range(30, 60, 5):
        now = ny(8, minute, day=26)
        assert earliest_send(now, job, state, policy) == max(planned, now)


def test_quiet_end_is_jittered() -> None:
    policy = Policy(jitter=seeded_jitter("s", 30))
    t = earliest_send(ny(23), cand(1), RecipientState(tz=NY, quiet=NIGHT), policy)
    assert ny(8, 30, day=26) <= t <= ny(9, 0, day=26)


# ---- Properties -------------------------------------------------------------------------------------

tzs = st.sampled_from([NY, UTC_TZ, ZoneInfo("Asia/Kolkata"), ZoneInfo("Pacific/Auckland")])
times_ = st.times().map(lambda t: t.replace(second=0, microsecond=0))
instants = st.datetimes(
    min_value=datetime(2026, 1, 1), max_value=datetime(2026, 12, 31), timezones=st.just(UTC)
)


@st.composite
def scenarios(draw: st.DrawFn) -> tuple[datetime, list[Candidate], RecipientState, Policy]:
    now = draw(instants)
    tz = draw(tzs)
    quiet = draw(st.none() | st.builds(QuietWindow, times_, times_))
    offsets = st.integers(min_value=-72 * 60, max_value=72 * 60).map(lambda m: now + timedelta(minutes=m))
    recent = tuple(sorted(draw(st.lists(offsets.filter(lambda t: t <= now), max_size=6))))
    state = RecipientState(
        tz=tz,
        quiet=quiet,
        outstanding=draw(st.integers(0, 3)),
        recent_prompts=recent,
        paused=draw(st.booleans()),
    )
    policy = Policy(
        max_outstanding=draw(st.integers(1, 3)),
        min_interval=timedelta(minutes=draw(st.sampled_from([0, 30, 120]))),
        max_per_day=draw(st.integers(1, 5)),
        jitter=seeded_jitter(draw(st.text(max_size=3)), draw(st.sampled_from([0, 30]))),
    )
    jobs = [
        Candidate(
            key=i,
            recipient_id="kate",
            priority=draw(st.floats(0, 1)),
            created_at=draw(offsets),
            not_before=draw(st.none() | offsets),
            expires_at=draw(st.none() | offsets),
            snoozed_until=draw(st.none() | offsets),
        )
        for i in range(draw(st.integers(0, 6)))
    ]
    return now, jobs, state, policy


@settings(max_examples=200, deadline=None)
@given(scenarios())
def test_plans_never_violate_constraints(
    scenario: tuple[datetime, list[Candidate], RecipientState, Policy],
) -> None:
    now, jobs, state, policy = scenario
    plan = decide(now, jobs, {"kate": state}, policy)
    by_key = {j.key: j for j in jobs}
    sent = [by_key[k] for k in plan.send]

    assert not set(plan.send) & set(plan.expire)
    assert all(j.expires_at is not None and j.expires_at <= now for j in (by_key[k] for k in plan.expire))
    if state.paused:
        assert sent == []
    assert state.outstanding + len(sent) <= max(policy.max_outstanding, state.outstanding)
    if sent:
        local = now.astimezone(state.tz)
        assert state.quiet is None or not state.quiet.contains(local.time())
        today = sum(1 for t in state.recent_prompts if t.astimezone(state.tz).date() == local.date())
        assert today + len(sent) <= policy.max_per_day
        if policy.min_interval:
            assert len(sent) == 1
            assert state.last_prompt is None or now - state.last_prompt >= policy.min_interval
    for job in sent:
        assert job.expires_at is None or job.expires_at > now
        assert job.not_before is None or job.not_before <= now
        assert job.snoozed_until is None or job.snoozed_until <= now
    if plan.wake_at is not None:
        assert plan.wake_at > now


@settings(max_examples=150, deadline=None)
@given(scenarios())
def test_earliest_send_is_never_in_the_past_or_in_quiet_hours(
    scenario: tuple[datetime, list[Candidate], RecipientState, Policy],
) -> None:
    now, jobs, state, policy = scenario
    for job in jobs:
        t = earliest_send(now, job, state, policy)
        assert t >= now
        assert state.quiet is None or not state.quiet.contains(t.astimezone(state.tz).time())
