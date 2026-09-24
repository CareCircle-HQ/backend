"""Normalise a vendor logo so it prints well on an invoice or quote.

A logo is only used in documents, so the target is PRINT rather than screen. That
drives every choice here:

FORMAT: always PNG out, whatever came in.
    * JPEG is lossy, and its artefacts cluster exactly where a logo has hard edges
      and small text -- the parts you notice on paper.
    * WebP is read fine by Pillow but is patchily supported by PDF toolchains, and
      a logo that silently fails to embed is worse than one that looks soft.
    * PNG is lossless, keeps transparency, and reportlab embeds it reliably.

SIZE: 600px on the long edge, and never upscaled.
    An invoice header shows a logo around 50mm wide. At 300 DPI -- the floor for
    something that should not look fuzzy in print -- 50mm is about 590px. Larger
    is bytes nobody sees; smaller is visibly soft on paper.

TRANSPARENCY is preserved rather than flattened onto white. Flattening looks
identical on a white invoice and wrong the first time a logo lands on a coloured
header or a dark PDF, which is a change nobody would think to re-test.

TRANSPARENT MARGINS ARE TRIMMED. Logos routinely arrive with a large empty border
baked in, and since a document sizes the image to a fixed box, that border makes
the artwork render small and off-centre for reasons the admin cannot see. Only
FULLY TRANSPARENT edges are cut: trimming white would destroy a white-on-dark logo,
and we cannot know which we have.
"""
import io
import logging

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# The long edge we normalise to -- see the module docstring.
TARGET_LONG_EDGE = 600

# Below this, the source simply has not got the detail to print crisply. Accepted
# with a warning rather than refused: a small logo is the only one some companies
# have, and refusing it leaves them with none at all.
MIN_USABLE_LONG_EDGE = 300

# Guards against a decompression bomb -- a small file that expands to a huge
# bitmap. Pillow warns above ~89M pixels; this is far stricter because a logo has
# no business being large.
MAX_SOURCE_PIXELS = 50_000_000


class LogoError(ValueError):
    """The upload is not usable as a logo. The message is shown to the vendor."""


def optimise(raw):
    """``(png_bytes, info)`` for a logo, ready to embed in a PDF.

    ``info`` carries the final ``width``/``height`` -- stored alongside the key so
    document layout can reserve the right box without opening the image -- and a
    ``warning`` when the source was too small to print crisply.

    Raises :class:`LogoError` with a message meant for the vendor.
    """
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as exc:  # noqa: BLE001 - any decode failure is one answer
        raise LogoError(
            "That file could not be read as an image. Try exporting it again as a "
            "PNG."
        ) from exc

    if image.width * image.height > MAX_SOURCE_PIXELS:
        raise LogoError("That image is too large. Please scale it down first.")

    # Phone cameras and some export tools record orientation in EXIF rather than
    # rotating the pixels. Without this a logo photographed on a phone embeds
    # sideways.
    image = ImageOps.exif_transpose(image)

    # RGBA for everything: it is the one mode that survives a palette with
    # transparency, a greyscale logo and a CMYK export from a print shop, all of
    # which turn up.
    if image.mode != "RGBA":
        image = image.convert("RGBA")

    image = _trim_transparent_border(image)
    if image.width == 0 or image.height == 0:
        raise LogoError("That image is completely transparent.")

    source_long_edge = max(image.width, image.height)
    warning = ""
    if source_long_edge < MIN_USABLE_LONG_EDGE:
        warning = (
            f"This logo is only {image.width}x{image.height} pixels, so it may look "
            f"soft when printed. A version at least {MIN_USABLE_LONG_EDGE} pixels "
            f"wide will look sharper."
        )

    # NEVER UPSCALE. Enlarging adds no detail and makes a small logo look worse --
    # blurry at a size that invites scrutiny, rather than small and sharp.
    if source_long_edge > TARGET_LONG_EDGE:
        scale = TARGET_LONG_EDGE / source_long_edge
        image = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            # LANCZOS: the sharpest of the downscalers, which is what logo edges
            # and small lettering need.
            Image.Resampling.LANCZOS,
        )

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue(), {
        "width": image.width,
        "height": image.height,
        "warning": warning,
    }


def _trim_transparent_border(image):
    """Crop fully transparent edges. Returns the image unchanged when opaque.

    ``getbbox`` on the alpha channel finds the artwork's real extent.
    """
    alpha = image.getchannel("A")
    # A fully opaque image has no border to trim, and getbbox would return the
    # whole frame anyway -- checked first to skip the work.
    if alpha.getextrema()[0] == 255:
        return image
    box = alpha.getbbox()
    if box is None:
        # Nothing visible at all; the caller turns this into a message.
        return image.crop((0, 0, 0, 0))
    return image.crop(box)
