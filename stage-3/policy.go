package main

import (
	"encoding/json"
	"net/http"
)

// Published booking policies (stage 3).
//
// A policy is a complete set of rules, not a patch, and it is immutable once
// published. Versions start at 1 per restaurant and are allocated only by a
// publication that actually commits, so a rejected write and a replay both leave the
// counter alone.

const (
	minPolicyMinutes  = 1
	maxPolicyMinutes  = 1440
	maxPolicyCutoff   = 10080
	minTableCapacity  = 1
	maxTableCapacity  = 100
	pathPolicySuffix  = "/policies"
	policyRequestPath = "/restaurants/%s/policies"
)

// validatePolicyValues checks a policy's values against the restaurant it belongs to.
// It runs on the way in from a request and again over an imported state, so a state
// can never hold a policy the API would have refused.
func validatePolicyValues(rest *restaurant, p *policy) *apiError {
	if _, ok := parseCivilDate(p.EffectiveFrom); !ok {
		return validationFailed("effective_from must be a calendar date as YYYY-MM-DD")
	}
	if p.SlotMinutes < minPolicyMinutes || p.SlotMinutes > maxPolicyMinutes {
		return validationFailed("slot_minutes must be an integer from 1 to 1440")
	}
	if p.DurationMinutes < minPolicyMinutes || p.DurationMinutes > maxPolicyMinutes {
		return validationFailed(
			"reservation_duration_minutes must be an integer from 1 to 1440")
	}
	if p.CutoffMinutes < 0 || p.CutoffMinutes > maxPolicyCutoff {
		return validationFailed(
			"cancellation_cutoff_minutes must be an integer from 0 to 10080")
	}
	if err := validateOpeningHours(p.OpeningHours); err != nil {
		return err
	}
	// A policy's opening hours carry one entry per weekday: unlike a fixture, the
	// requirements are explicit about that here.
	seenDay := map[string]bool{}
	for _, hours := range p.OpeningHours {
		if seenDay[hours.Weekday] {
			return validationFailed("opening_hours must not repeat a weekday")
		}
		seenDay[hours.Weekday] = true
	}
	// `capacities` names exactly the restaurant's tables: no extras, none missing.
	if len(p.Capacities) != len(rest.Tables) {
		return validationFailed("capacities must name exactly this restaurant's tables")
	}
	for _, t := range rest.Tables {
		seats, named := p.Capacities[t.ID]
		if !named {
			return validationFailed("capacities is missing table " + t.ID)
		}
		if seats < minTableCapacity || seats > maxTableCapacity {
			return validationFailed("each capacity must be an integer from 1 to 100")
		}
	}
	return nil
}

// parsePolicyBody reads a complete policy from a request body.
//
// Every field is required and every one of them is validated as a value rather than as
// a type: the endpoint says an invalid policy is 422, and §5's own precedent is that an
// endpoint-specific field rule outranks the generic wrong-type 400. A boolean in an
// integer field is therefore an invalid policy, not a malformed request.
func parsePolicyBody(body *jsonBody) (*policy, *apiError) {
	p := &policy{}

	raw, present := body.members["effective_from"]
	if !present || jsonKind(raw) != "string" {
		return nil, validationFailed("effective_from must be a calendar date as YYYY-MM-DD")
	}
	if err := json.Unmarshal(raw, &p.EffectiveFrom); err != nil {
		return nil, validationFailed("effective_from must be a calendar date as YYYY-MM-DD")
	}

	for _, field := range []struct {
		name  string
		into  *int
		lower int
		upper int
	}{
		{"slot_minutes", &p.SlotMinutes, minPolicyMinutes, maxPolicyMinutes},
		{"reservation_duration_minutes", &p.DurationMinutes, minPolicyMinutes, maxPolicyMinutes},
		{"cancellation_cutoff_minutes", &p.CutoffMinutes, 0, maxPolicyCutoff},
	} {
		value, present, aerr := body.integerMember(field.name)
		if aerr != nil {
			return nil, validationFailed(field.name + " must be an integer")
		}
		if !present {
			return nil, validationFailed(field.name + " is required")
		}
		if value < field.lower || value > field.upper {
			return nil, validationFailed(field.name + " is out of range")
		}
		*field.into = value
	}

	raw, present = body.members["opening_hours"]
	if !present || jsonKind(raw) != "array" {
		return nil, validationFailed("opening_hours must be an array")
	}
	if err := json.Unmarshal(raw, &p.OpeningHours); err != nil {
		return nil, validationFailed("opening_hours must be weekday, opens and closes")
	}

	raw, present = body.members["capacities"]
	if !present || jsonKind(raw) != "object" {
		return nil, validationFailed("capacities must be an object of table id to seats")
	}
	if err := json.Unmarshal(raw, &p.Capacities); err != nil {
		return nil, validationFailed("each capacity must be an integer from 1 to 100")
	}
	return p, nil
}

// handlePublishPolicy is POST /restaurants/{id}/policies.
//
// The order is the one §7 fixes and §policies extends: parse the body, authenticate,
// resolve the key, and only then look at the restaurant, the caller's permission and
// the policy itself.
func (s *server) handlePublishPolicy(w http.ResponseWriter, r *http.Request, restaurantID string) {
	path := "/restaurants/" + restaurantID + pathPolicySuffix
	s.serveIdempotent(w, r, path, func(caller *user, body *jsonBody) (int, any, *apiError) {
		rest := s.st.restaurantsByID[restaurantID]
		if rest == nil {
			return 0, nil, notFound()
		}
		if !rest.manages(caller.ID) {
			return 0, nil, errorf(http.StatusForbidden, "forbidden",
				"only a manager of this restaurant may publish its policies")
		}
		published, aerr := parsePolicyBody(body)
		if aerr != nil {
			return 0, nil, aerr
		}
		if aerr := validatePolicyValues(rest, published); aerr != nil {
			return 0, nil, aerr
		}
		// The version is taken here, on the way to a commit, so a refused write
		// above has allocated nothing.
		published.PolicyVersion = s.st.nextPolicyVersion(rest.ID)
		s.st.addPolicy(rest.ID, published)
		return http.StatusCreated, published, nil
	})
}

// handleListPolicies is GET /restaurants/{id}/policies: public, in publication order,
// and without policy 0, which is the fixture's own rules rather than a publication.
func (s *server) handleListPolicies(w http.ResponseWriter, r *http.Request, restaurantID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	rest := s.st.restaurantsByID[restaurantID]
	if rest == nil {
		writeError(w, notFound())
		return
	}
	published := s.st.policiesOf(rest.ID)
	listed := make([]any, 0, len(published))
	for _, p := range published {
		listed = append(listed, p)
	}
	writeJSON(w, http.StatusOK, map[string]any{"policies": listed})
}
