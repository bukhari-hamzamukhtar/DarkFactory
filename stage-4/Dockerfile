# Build stage. Dependencies are vendored in the repository, so the build needs no
# module downloads; the runtime stage then needs no network at all (§2).
FROM golang:1.23-alpine AS build

WORKDIR /src
COPY go.mod go.sum ./
COPY vendor ./vendor
COPY *.go ./
# The browser product is embedded into the binary, so it has to be in the build
# context: nothing is fetched at runtime (§2).
COPY web ./web

# A static binary: the runtime image has no libc to link against.
RUN CGO_ENABLED=0 go build -mod=vendor -trimpath -ldflags "-s -w" -o /out/tablekeeper .

# Runtime stage. Nothing but the binary: the IANA timezone database is compiled into
# it (time/tzdata), so no OS package and no outbound request is needed to resolve a
# restaurant's local time.
FROM scratch

COPY --from=build /out/tablekeeper /tablekeeper

ENV PORT=8080
EXPOSE 8080

ENTRYPOINT ["/tablekeeper"]
