import os
from typing import Optional
from pypdf import PdfWriter
from pypdf.constants import UserAccessPermissions as UAP
from app.core.logger import get_logger

logger = get_logger(__name__)


class PDFProtector:
    """Encrypt and restrict a PDF file with password protection."""

    def __init__(self, input_path: str):
        self.input_path = input_path
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"Input file not found: {input_path}")

    def protect(
        self,
        output_path: str,
        user_password: str,
        owner_password: Optional[str] = None,
        allow_printing: bool = True,
        allow_modifying: bool = True,
        allow_copying: bool = True,
        allow_annotating: bool = True,
        allow_form_filling: bool = True,
        allow_accessibility_extraction: bool = True,
        allow_assembly: bool = True,
        allow_print_to_representation: bool = True,
    ) -> str:
        """
        Encrypt the PDF with the given password and permission settings.

        Args:
            output_path: Path to save the encrypted PDF.
            user_password: Password required to open the document.
            owner_password: Optional password for changing permissions.
            allow_*: Boolean flags for various PDF permissions.

        Returns:
            Path to the encrypted PDF file.
        """
        writer = PdfWriter()
        writer.append(self.input_path)

        # Build permissions bitmask (default allows everything)
        permissions = (
            UAP.PRINT
            | UAP.MODIFY
            | UAP.EXTRACT
            | UAP.ADD_OR_MODIFY
            | UAP.FILL_FORM_FIELDS
            | UAP.EXTRACT_TEXT_AND_GRAPHICS
            | UAP.ASSEMBLE_DOC
            | UAP.PRINT_TO_REPRESENTATION
        )

        if not allow_printing:
            permissions &= ~UAP.PRINT
            permissions &= ~UAP.PRINT_TO_REPRESENTATION
        if not allow_modifying:
            permissions &= ~UAP.MODIFY
        if not allow_copying:
            permissions &= ~UAP.EXTRACT
        if not allow_annotating:
            permissions &= ~UAP.ADD_OR_MODIFY
        if not allow_form_filling:
            permissions &= ~UAP.FILL_FORM_FIELDS
        if not allow_accessibility_extraction:
            permissions &= ~UAP.EXTRACT_TEXT_AND_GRAPHICS
        if not allow_assembly:
            permissions &= ~UAP.ASSEMBLE_DOC
        if not allow_print_to_representation:
            permissions &= ~UAP.PRINT_TO_REPRESENTATION

        # Use owner password = user password if not provided
        effective_owner = owner_password if owner_password else user_password

        writer.encrypt(
            user_password=user_password,
            owner_password=effective_owner,
            use_128bit=True,
            permissions_flag=permissions,
        )

        with open(output_path, "wb") as f:
            writer.write(f)

        logger.info(f"PDF protected: {output_path}")
        return output_path

    @staticmethod
    def validate_password_strength(password: str) -> dict:
        """Return password strength analysis."""
        has_lower = any(c.islower() for c in password)
        has_upper = any(c.isupper() for c in password)
        has_digit = any(c.isdigit() for c in password)
        has_special = any(c in "!@#$%^&*()_+-=[]{}|;':\",./<>?" for c in password)
        length = len(password)

        score = sum([has_lower, has_upper, has_digit, has_special]) + (1 if length >= 8 else 0)
        strength = "weak"
        if score >= 5:
            strength = "strong"
        elif score >= 3:
            strength = "medium"

        return {
            "length": length,
            "has_lower": has_lower,
            "has_upper": has_upper,
            "has_digit": has_digit,
            "has_special": has_special,
            "strength": strength,
        }
