from io import BytesIO

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    status,
)
from PIL import Image
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config import (
    get_jwt_auth_manager,
    get_s3_storage_client,
)
from database import get_db, UserModel, UserGroupEnum
from database.models.accounts import (
    UserProfileModel,
    GenderEnum,
)
from exceptions import BaseSecurityError
from schemas.profiles import (
    ProfileCreateSchema,
    ProfileResponseSchema,
)
from security.interfaces import JWTAuthManagerInterface
from storages.interfaces import S3StorageInterface


router = APIRouter()


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    jwt_manager: JWTAuthManagerInterface = Depends(
        get_jwt_auth_manager
    ),
) -> UserModel:

    authorization = request.headers.get("Authorization")

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header is missing",
        )

    scheme, _, token = authorization.partition(" ")

    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Invalid Authorization header format. "
                "Expected 'Bearer <token>'"
            ),
        )

    try:
        payload = jwt_manager.decode_access_token(token)
    except BaseSecurityError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(error),
        ) from error

    current_user_id = payload.get("user_id")

    if not isinstance(current_user_id, int):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access token.",
        )

    stmt = (
        select(UserModel)
        .options(selectinload(UserModel.group))
        .where(UserModel.id == current_user_id)
    )

    result = await db.execute(stmt)
    current_user = result.scalars().first()

    if not current_user or not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or not active.",
        )

    return current_user


@router.post(
    "/users/{user_id}/profile/",
    response_model=ProfileResponseSchema,
    status_code=status.HTTP_201_CREATED,
    summary="Create User Profile",
)
async def create_user_profile(
    user_id: int,
    current_user: UserModel = Depends(get_current_user),
    profile_data: ProfileCreateSchema = Depends(
        ProfileCreateSchema.as_form
    ),
    db: AsyncSession = Depends(get_db),
    s3_client: S3StorageInterface = Depends(
        get_s3_storage_client
    ),
) -> ProfileResponseSchema:

    # Check target user
    stmt = select(UserModel).where(UserModel.id == user_id)
    result = await db.execute(stmt)
    target_user = result.scalars().first()

    if not target_user or not target_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or not active.",
        )

    # Check permissions
    if (
        current_user.id != user_id
        and not current_user.has_group(UserGroupEnum.ADMIN)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to edit this profile.",
        )

    # Check existing profile
    stmt = select(UserProfileModel).where(
        UserProfileModel.user_id == user_id
    )
    result = await db.execute(stmt)
    existing_profile = result.scalars().first()

    if existing_profile:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User already has a profile.",
        )

    # Upload avatar to S3
    avatar_bytes = await profile_data.avatar.read()

    with Image.open(BytesIO(avatar_bytes)) as image:
        extension = ".png" if image.format == "PNG" else ".jpg"

    avatar_key = f"avatars/{user_id}_avatar{extension}"

    try:
        await s3_client.upload_file(
            avatar_key,
            avatar_bytes,
        )

        avatar_url = await s3_client.get_file_url(
            avatar_key
        )

    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to upload avatar. Please try again later.",
        ) from error

    # Create profile in database
    try:
        new_profile = UserProfileModel(
            user_id=user_id,
            first_name=profile_data.first_name,
            last_name=profile_data.last_name,
            gender=GenderEnum(profile_data.gender),
            date_of_birth=profile_data.date_of_birth,
            info=profile_data.info,
            avatar=avatar_key,
        )

        db.add(new_profile)
        await db.flush()

        profile_id = new_profile.id

        await db.commit()

    except SQLAlchemyError as error:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred during profile creation.",
        ) from error

    return ProfileResponseSchema(
        id=profile_id,
        user_id=user_id,
        first_name=profile_data.first_name,
        last_name=profile_data.last_name,
        gender=profile_data.gender,
        date_of_birth=profile_data.date_of_birth,
        info=profile_data.info,
        avatar=avatar_url,
    )
