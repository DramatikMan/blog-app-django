from datetime import datetime
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import app.routers.spotify.config as config
from .types import CurrentSong, CurrentSongResp, Status, URL
from .utils import pause_song, play_song, skip_song, spotify_api_request
from ...db.models import Room, SpotifyTokens, Vote
from ...db.utils import get_tokens, update_or_create_tokens
from ...dependencies import get_db_session, get_session
from ...types import SpotifyAuthResp


router = APIRouter()


@router.get('/auth/url')
async def get_auth_url() -> URL:
    scopes = (
        'user-read-playback-state',
        'user-modify-playback-state',
        'user-read-currently-playing'
    )

    payload = dict(
        scope=' '.join(scopes),
        response_type='code',
        redirect_uri=config.REDIRECT_URI,
        client_id=config.CLIENT_ID
    )

    req = httpx.Request(method='GET', url=config.AUTH_URI, params=payload)

    return URL(url=str(req.url))


@router.get('/auth/status')
async def get_auth_status(
    session: dict[Any, Any] = Depends(get_session),
    DB: AsyncSession = Depends(get_db_session)
) -> Status:
    tokens: Optional[SpotifyTokens] = await get_tokens(DB, session['identity'])

    if tokens:
        if tokens.expiry_dt <= datetime.now():
            payload = dict(
                grant_type='refresh_token',
                refresh_token=tokens.refresh_token,
                cliend_id=config.CLIENT_ID,
                client_secret=config.CLIENT_SECRET
            )

            async with httpx.AsyncClient() as client:
                resp: httpx.Response = await client.post(
                    url=config.TOKEN_URI,
                    data=payload
                )
                await update_or_create_tokens(
                    DB,
                    session['identity'],
                    resp.json()
                )

        return Status(status=True)

    return Status(status=False)


@router.get('/redirect', response_class=RedirectResponse)
async def get_redirect(
    code: str,
    session: dict[Any, Any] = Depends(get_session),
    DB: AsyncSession = Depends(get_db_session)
) -> str:
    payload = dict(
        grant_type='authorization_code',
        code=code,
        redirect_uri=config.REDIRECT_URI,
        client_id=config.CLIENT_ID,
        client_secret=config.CLIENT_SECRET
    )

    async with httpx.AsyncClient() as client:
        resp: httpx.Response = await client.post(
            config.TOKEN_URI,
            data=payload
        )
        resp_data = SpotifyAuthResp(**resp.json())

    await update_or_create_tokens(DB, session['identity'], resp_data)

    return '/'


@router.get('/song')
async def get_song(
    session: dict[Any, Any] = Depends(get_session),
    DB: AsyncSession = Depends(get_db_session)
) -> CurrentSong:
    stmt = select(Room).where(Room.code == session['room_code'])
    result = await DB.execute(stmt)
    room: Optional[Room] = result.scalar_one_or_none()

    if room is None:
        raise HTTPException(status_code=404)

    data: CurrentSongResp = await spotify_api_request(DB, room.host)
    room_code, votes_to_skip, current_song = \
        room.code, room.votes_to_skip, room.current_song

    item = data.item
    artists = ''

    for i, artist in enumerate(item.artists):
        if i > 0:
            artists += ', '

        artists += artist.name

    stmt = select(Vote).where(Vote.room_code == room_code)
    result = await DB.execute(stmt)
    votes_qty: int = len(result.scalars().all())

    song = CurrentSong(
        title=item.name,
        artist=artists,
        duration=item.duration_ms,
        progress=data.progress_ms,
        image_url=item.album.images[0].url,
        is_playing=data.is_playing,
        votes=votes_qty,
        votes_required=votes_to_skip,
        id=item.id
    )

    if current_song != song.id:
        await DB.execute(
            update(Room)
            .where(Room.code == session['room_code'])
            .values(current_song=song.id)
        )
        await DB.execute(delete(Vote).where(Vote.room_code == room_code))

    await DB.commit()

    return song


@router.put('/pause', status_code=204)
async def put_pause(
    session: dict[Any, Any] = Depends(get_session),
    DB: AsyncSession = Depends(get_db_session)
) -> None:
    stmt = select(Room).where(Room.code == session['room_code'])
    result = await DB.execute(stmt)
    room: Optional[Room] = result.scalar_one_or_none()

    if room is None:
        raise HTTPException(status_code=404)

    if session['identity'] == room.host or room.guest_can_pause:
        await pause_song(DB, room.host)

        return None

    raise HTTPException(status_code=403)


@router.put('/play', status_code=204)
async def put_play(
    session: dict[Any, Any] = Depends(get_session),
    DB: AsyncSession = Depends(get_db_session)
) -> None:
    stmt = select(Room).where(Room.code == session['room_code'])
    result = await DB.execute(stmt)
    room: Optional[Room] = result.scalar_one_or_none()

    if room is None:
        raise HTTPException(status_code=404)

    if session['identity'] == room.host or room.guest_can_pause:
        await play_song(DB, room.host)

        return None

    raise HTTPException(status_code=403)


@router.post('/skip', status_code=204)
async def post_skip(
    session: dict[Any, Any] = Depends(get_session),
    DB: AsyncSession = Depends(get_db_session)
) -> None:
    stmt = select(Room).where(Room.code == session['room_code'])
    result = await DB.execute(stmt)
    room: Optional[Room] = result.scalar_one_or_none()

    if room is None:
        raise HTTPException(status_code=404)

    votes_needed: int = room.votes_to_skip
    result = await DB.execute(select(Vote).where(
        Vote.room_code == room.code and
        Vote.song_id == room.current_song
    ))
    all_votes: list[Vote] = result.scalars().all()

    result = await DB.execute(select(Vote).where(
        Vote.room_code == room.code and
        Vote.song_id == room.current_song and
        Vote.user == session['identity']
    ))
    user_votes: list[Vote] = result.scalars().all()

    if session['identity'] == room.host:
        await DB.execute(delete(Vote).where(
            Vote.room_code == room.code and
            Vote.song_id == room.current_song
        ))
        await skip_song(DB, session['identity'])
    elif len(user_votes) == 0:
        if (len(all_votes) + 1) >= votes_needed:
            await DB.execute(delete(Vote).where(
                Vote.room_code == room.code and
                Vote.song_id == room.current_song
            ))
            await skip_song(DB, room.host)
        else:
            DB.add(Vote(
                user=session['identity'],
                room_code=room.code,
                song_id=room.current_song
            ))

    await DB.commit()

    return None
