package main

import (
	"time"
	_ "time/tzdata" // the IANA database travels inside the binary: §2 forbids runtime network
)

const (
	localStampLayout = "2006-01-02T15:04"
	localDateLayout  = "2006-01-02"
	// offsetLayout is RFC 3339 with an explicit numeric offset, never "Z" (§3.4).
	offsetLayout = "2006-01-02T15:04:05-07:00"
	// fractionalOffsetLayout is the same, keeping any sub-second precision the
	// original had and printing none when it had none.
	fractionalOffsetLayout = "2006-01-02T15:04:05.999999999-07:00"
)

// weekdayKeys indexes the fixture's weekday names by time.Weekday.
var weekdayKeys = [7]string{"sun", "mon", "tue", "wed", "thu", "fri", "sat"}

func isWeekdayKey(name string) bool {
	for _, key := range weekdayKeys {
		if key == name {
			return true
		}
	}
	return false
}

// civilDate is a calendar date with no zone: what §8's `date` parameter means.
type civilDate struct {
	year  int
	month time.Month
	day   int
}

// civilStamp is a wall-clock instant at a restaurant: a date plus minutes from local
// midnight. It is deliberately not a time.Time, because a local stamp in a skipped
// hour names no instant at all and must stay representable until it is resolved.
type civilStamp struct {
	date    civilDate
	minutes int
}

// parseCivilDate accepts exactly YYYY-MM-DD and a date that really exists.
func parseCivilDate(text string) (civilDate, bool) {
	parsed, err := time.Parse(localDateLayout, text)
	if err != nil || parsed.Format(localDateLayout) != text {
		return civilDate{}, false
	}
	return civilDate{parsed.Year(), parsed.Month(), parsed.Day()}, true
}

// parseCivilStamp accepts exactly YYYY-MM-DDTHH:MM -- no seconds, no offset, no `Z`
// (§8). Anything else is a validation failure, not a different instant.
func parseCivilStamp(text string) (civilStamp, bool) {
	parsed, err := time.Parse(localStampLayout, text)
	if err != nil || parsed.Format(localStampLayout) != text {
		return civilStamp{}, false
	}
	date := civilDate{parsed.Year(), parsed.Month(), parsed.Day()}
	return civilStamp{date, parsed.Hour()*60 + parsed.Minute()}, true
}

func (s civilStamp) String() string {
	return s.asUTC().Format(localStampLayout)
}

// asUTC is the stamp read as if the zone were UTC: the pivot for offset arithmetic.
func (s civilStamp) asUTC() time.Time {
	return time.Date(s.date.year, s.date.month, s.date.day, 0, 0, 0, 0, time.UTC).
		Add(time.Duration(s.minutes) * time.Minute)
}

func (d civilDate) weekdayKey() string {
	noon := time.Date(d.year, d.month, d.day, 12, 0, 0, 0, time.UTC)
	return weekdayKeys[int(noon.Weekday())]
}

func (d civilDate) String() string {
	return time.Date(d.year, d.month, d.day, 0, 0, 0, 0, time.UTC).Format(localDateLayout)
}

func (d civilDate) at(minutes int) civilStamp { return civilStamp{d, minutes} }

// plusDays walks the calendar, which is what a recurring agreement does: occurrence i
// starts on the anchor's local date plus i intervals of seven days, at the same local
// clock time. Adding days to a calendar date cannot be done by adding hours to an
// instant, because a transition in between would move the clock.
func (d civilDate) plusDays(days int) civilDate {
	noon := time.Date(d.year, d.month, d.day, 12, 0, 0, 0, time.UTC).AddDate(0, 0, days)
	return civilDate{noon.Year(), noon.Month(), noon.Day()}
}

// resolveLocal turns a wall-clock stamp at a restaurant into an absolute instant.
//
// Three outcomes, which are exactly §9's three cases:
//   - one instant: the ordinary case;
//   - no instant: the stamp falls in a skipped hour (spring forward), ok is false;
//   - two instants: the stamp falls in a repeated hour (fall back), and the first
//     occurrence -- the one before the clocks change -- is returned.
//
// The candidate offsets are sampled a day either side of the stamp, which brackets
// any transition, and each candidate is kept only if it reads back as the very same
// wall clock. That is what makes a skipped hour detectable rather than silently
// shifted, and it holds for any zone rule rather than the two in the table.
func resolveLocal(loc *time.Location, stamp civilStamp) (time.Time, bool) {
	pivot := stamp.asUTC()
	var best time.Time
	found := false
	for _, probe := range []time.Time{pivot.Add(-24 * time.Hour), pivot, pivot.Add(24 * time.Hour)} {
		_, offset := probe.In(loc).Zone()
		candidate := pivot.Add(-time.Duration(offset) * time.Second)
		if candidate.In(loc).Format(localStampLayout) != stamp.String() {
			continue
		}
		if !found || candidate.Before(best) {
			best, found = candidate, true
		}
	}
	return best, found
}

// localReading renders an instant as a bare wall clock: the restaurant's own date
// and time with the zone dropped, so that two readings compare the way a clock on
// the wall reads rather than the way time actually passes.
func localReading(instant time.Time, loc *time.Location) time.Time {
	local := instant.In(loc)
	return time.Date(local.Year(), local.Month(), local.Day(),
		local.Hour(), local.Minute(), local.Second(), 0, time.UTC)
}

// instantReaching returns the instant at which the restaurant's clock reaches a
// wall-clock stamp.
//
// Usually that is the instant the stamp names, and a repeated stamp is reached at
// its first occurrence -- both of which resolveLocal already gives. The case it does
// not cover is a stamp inside a skipped hour: no clock in the building ever reads
// it, yet a restaurant whose closing time falls there still closes, at the moment
// the clock jumps past it. That is what the search below finds.
//
// Only closing times need this. A bookable slot in a skipped hour does not exist at
// all (§9) and is dropped long before here.
func instantReaching(loc *time.Location, stamp civilStamp) time.Time {
	if instant, exists := resolveLocal(loc, stamp); exists {
		return instant
	}
	// The reading is skipped, so a forward transition sits between the day before
	// and the day after, and across that window the clock only moves forward. The
	// first instant whose reading is at or past the stamp can therefore be
	// bisected for.
	target := stamp.asUTC()
	low := stamp.date.at(0).asUTC().Add(-24 * time.Hour)
	high := low.Add(72 * time.Hour)
	for high.Sub(low) > time.Second {
		middle := low.Add(high.Sub(low) / 2)
		if localReading(middle, loc).Before(target) {
			low = middle
		} else {
			high = middle
		}
	}
	return high
}

// normalizeTimestamp accepts any RFC 3339 timestamp and renders it the way §3.4
// requires a response to read: with an explicit numeric offset. A fixture that writes
// "Z" names the same instant as one that writes "+00:00", and must not be refused over
// the spelling.
//
// A timestamp that already carries a numeric offset is kept exactly as it was given,
// and one that does not keeps whatever sub-second precision it had. §10 says supplied
// timestamps must not be regenerated, and truncating .250 to .000 would change the
// instant a fixture named.
func normalizeTimestamp(text string) (string, time.Time, bool) {
	instant, err := time.Parse(time.RFC3339, text)
	if err != nil {
		return "", time.Time{}, false
	}
	if hasNumericOffset(text) {
		return text, instant, true
	}
	return instant.Format(fractionalOffsetLayout), instant, true
}

// hasNumericOffset reports whether a timestamp ends in +HH:MM or -HH:MM, which is the
// form §3.4 asks a response to use.
func hasNumericOffset(text string) bool {
	if len(text) < 6 {
		return false
	}
	tail := text[len(text)-6:]
	if tail[0] != '+' && tail[0] != '-' {
		return false
	}
	if tail[3] != ':' {
		return false
	}
	for _, index := range []int{1, 2, 4, 5} {
		if tail[index] < '0' || tail[index] > '9' {
			return false
		}
	}
	return true
}

// formatInstant renders an absolute instant as the restaurant reads it (§3.4).
func formatInstant(instant time.Time, loc *time.Location) string {
	return instant.In(loc).Format(offsetLayout)
}

func formatUTC(instant time.Time) string {
	return instant.UTC().Format(offsetLayout)
}

// parseHourMinute accepts a fixture's local HH:MM and returns minutes from midnight.
// `closes` is allowed to be 24:00, which names the end of the local day; §4 promises
// opening hours never cross midnight, so nothing later can be meant.
func parseHourMinute(text string) (int, bool) {
	if len(text) != 5 || text[2] != ':' {
		return 0, false
	}
	hours, ok := twoDigits(text[0:2])
	if !ok {
		return 0, false
	}
	minutes, ok := twoDigits(text[3:5])
	if !ok {
		return 0, false
	}
	if minutes > 59 || hours > 24 || (hours == 24 && minutes != 0) {
		return 0, false
	}
	return hours*60 + minutes, true
}

func twoDigits(text string) (int, bool) {
	value := 0
	for i := 0; i < len(text); i++ {
		if text[i] < '0' || text[i] > '9' {
			return 0, false
		}
		value = value*10 + int(text[i]-'0')
	}
	return value, true
}
