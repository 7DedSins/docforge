# Gotenberg plus the font substitute its base image is missing.
#
# LibreOffice replaces fonts a document asks for but the system doesn't have.
# That is only safe when the replacement is *metric-compatible* — identical
# character widths, so line breaks and page breaks land where the author put
# them. The base image ships two such clones and not a third:
#
#     Calibri  -> Carlito      metric-compatible   (fonts-crosextra-carlito)
#     Cambria  -> Caladea      metric-compatible   (fonts-crosextra-caladea)
#     Georgia  -> Noto Serif   NOT compatible      <- silently reflows
#
# Georgia is common in real documents, so that gap produces output that looks
# subtly wrong versus Word — different line breaks, shifted spacing — with no
# error to explain why. Gelasio is the metric-compatible Georgia substitute.
#
# Licensing matters here if you are selling access: Gelasio is SIL OFL and free
# to redistribute. Actual Georgia ships via ttf-mscorefonts-installer under a
# Microsoft EULA, which is not clean for a commercial service. Metric clones
# keep the output faithful and the licence tidy.

FROM gotenberg/gotenberg:8

USER root

# Static instances, not the variable build. LibreOffice's variable-font support
# is limited — given only a variable file it renders the default instance and
# synthesises faux bold, which defeats the point of installing a metric clone.
ADD https://raw.githubusercontent.com/SorkinType/Gelasio/main/fonts/ttf/Gelasio-Regular.ttf \
    /usr/share/fonts/truetype/gelasio/Gelasio-Regular.ttf
ADD https://raw.githubusercontent.com/SorkinType/Gelasio/main/fonts/ttf/Gelasio-Bold.ttf \
    /usr/share/fonts/truetype/gelasio/Gelasio-Bold.ttf
ADD https://raw.githubusercontent.com/SorkinType/Gelasio/main/fonts/ttf/Gelasio-Italic.ttf \
    /usr/share/fonts/truetype/gelasio/Gelasio-Italic.ttf
ADD https://raw.githubusercontent.com/SorkinType/Gelasio/main/fonts/ttf/Gelasio-BoldItalic.ttf \
    /usr/share/fonts/truetype/gelasio/Gelasio-BoldItalic.ttf

# Ship the licence alongside the fonts, as OFL requires.
ADD https://raw.githubusercontent.com/google/fonts/main/ofl/gelasio/OFL.txt \
    /usr/share/fonts/truetype/gelasio/OFL.txt

# ADD creates the parent directory with no traverse permission for anyone but
# root. The files land correctly and are then unreadable by the gotenberg user
# at runtime, so fontconfig silently ignores them. Fix the directory, not just
# the files.
RUN chmod 755 /usr/share/fonts/truetype/gelasio && \
    chmod 644 /usr/share/fonts/truetype/gelasio/* && \
    fc-cache -f >/dev/null

USER gotenberg

# Verify as the runtime user, not as root. Checking this while still root is
# worse than not checking: root can read the directory regardless of its mode,
# so the assertion passes and ships an image where the fonts are invisible to
# the process that actually needs them.
RUN fc-list | grep -qi gelasio || \
      (echo "BUILD FAILED: Gelasio not visible to the gotenberg user" && exit 1) && \
    fc-match Georgia | grep -qi gelasio || \
      (echo "BUILD FAILED: Georgia does not resolve to Gelasio" && exit 1)
