"""Shared page title, separate from machine configuration and collection."""
import json
from pydantic import BaseModel, ConfigDict, Field, field_validator
from .store import atomic_json

DEFAULT_TITLE = 'Your machine floor'

class TitleInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    title: str = Field(min_length=1, max_length=80)
    revision: int = Field(ge=0)

    @field_validator('title')
    @classmethod
    def clean_title(cls, value):
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('Use a single-line title')
        value = value.strip()
        if not value:
            raise ValueError('Enter a page title')
        return value

class UISettings:
    def __init__(self, path):
        self.path = path
        self.data = {'version':1, 'title':DEFAULT_TITLE, 'revision':0}
        if path.exists():
            data = json.loads(path.read_text())
            if data.get('version') != 1:
                raise ValueError('Unsupported UI settings version')
            value = TitleInput.model_validate({k:data[k] for k in ('title','revision')})
            self.data.update(value.model_dump())

    def view(self):
        return {k:self.data[k] for k in ('title','revision')}

    def save(self, value):
        if value.revision != self.data['revision']:
            raise ValueError('Page title changed; reopen the editor and try again')
        if value.title != self.data['title']:
            replacement = {'version':1, 'title':value.title, 'revision':value.revision+1}
            atomic_json(self.path, replacement)
            self.data = replacement
        return self.view()
