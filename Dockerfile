FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# libdvd-pkg lives in Debian's "contrib" component, which isn't enabled
# by default — turn it on for both possible sources.list formats before
# apt-get update. Handles the actual bookworm default line shape
# ("... main non-free-firmware") as well as a bare trailing "main".
RUN if [ -f /etc/apt/sources.list ]; then \
        sed -i -E 's/ main / main contrib /; s/ main$/ main contrib/' /etc/apt/sources.list; \
    fi \
    && if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i -E 's/^(Components:.*\bmain\b)/\1 contrib/' /etc/apt/sources.list.d/debian.sources; \
    fi \
    && grep -r contrib /etc/apt/sources.list* || (echo "Failed to enable contrib component" && exit 1)

# dvdbackup + libdvd-pkg (builds libdvdcss locally, since Debian/Ubuntu
# don't ship it precompiled) give us CSS-decrypting reads.
# genisoimage builds the final unencrypted .iso from the decrypted mirror.
# python3 + flask run the supervisor/web UI; ffmpeg grabs screenshots;
# libarchive-tools (bsdtar) backs the drive-less mock mode.
#
# libdvdcss gotchas (each one has silently produced a CSS-less image before):
# - dpkg-reconfigure may POSTPONE the libdvdcss build "till after next APT
#   operation"; the --reinstall of libdvd-pkg triggers that hook and forces
#   the build through.
# - NO purge/autoremove afterwards: libdvd-pkg depends on wget +
#   build-essential, so purging them cascades into removing libdvd-pkg,
#   whose removal hook purges the libdvdcss2 it just built. autoremove can
#   also collect libdvdcss2 (it's marked "automatically installed"), hence
#   the apt-mark. The extra image size is the price of a working image.
# - The ldconfig check runs LAST so any regression fails the build loudly
#   instead of shipping an image that rips only unencrypted discs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        dvdbackup \
        genisoimage \
        libdvd-pkg \
        util-linux \
        udev \
        eject \
        python3 \
        python3-flask \
        ffmpeg \
        libarchive-tools \
        ca-certificates \
    && dpkg-reconfigure libdvd-pkg \
    && apt-get install -y --reinstall libdvd-pkg \
    && (apt-mark manual libdvdcss2 || true) \
    && rm -rf /var/lib/apt/lists/* \
    && (ldconfig -p | grep -q libdvdcss \
        || (echo "ERROR: libdvdcss missing after build" && exit 1))

WORKDIR /app
COPY ripper/ /app/ripper/
COPY vendor/ /app/vendor/

# Defaults — override at `docker run` time (see README).
ENV DVD_DEVICES=/dev/sr0
ENV POLL_INTERVAL=5
ENV OUTPUT_DIR=/mnt/rips
ENV WORK_DIR=/var/tmp/dvd-autorip
ENV PORT=8080
ENV DUPLICATE_POLICY=skip
ENV DISCORD_WEBHOOK_URL=

VOLUME ["/mnt/rips"]
EXPOSE 8080

ENTRYPOINT ["python3", "-m", "ripper.main"]
