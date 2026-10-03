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
