"""Sample implementation for ActionMixin."""

import io
import inspect, json, logging

from plugin import InvenTreePlugin
from plugin.mixins import ActionMixin, APICallMixin, SettingsMixin, EventMixin
from company.models import Company, SupplierPriceBreak
from part.models import Part, SupplierPart, PartCategory, PartParameterTemplate, PartParameter, BomItem, BomItemSubstitute
from stock.models import StockItem
from InvenTree.helpers_model import download_image_from_url
from django.core.files.base import ContentFile
from InvenTree.tasks import offload_task

logger = logging.getLogger("rebrickableplugin")

class RebrickablePlugin(ActionMixin, APICallMixin, SettingsMixin, InvenTreePlugin):
    """An action plugin which offers variuous integrations with Rebrickable."""

    NAME = 'RebrickablePlugin'
    SLUG = 'rebrickable'
    ACTION_NAME = 'rebrickable'

    SETTINGS = {
        "API_TOKEN": {
            "name": "Rebrickable API Token",
            "protected": True,
            "required": True,
        },
        'LEGO_CATEGORY_ID': {
            'name': 'Category for filament parts',
            'description': 'Where is your API located?',
            "model": "part.partcategory",
        },
    }

    result = {}
    category_map = {}

    def import_category(self, id: int, parent: PartCategory) -> PartCategory:

        if id in self.category_map:
            return self.category_map[id]
        else:

            headers = {
                "Authorization": "key " + self.get_setting('API_TOKEN')
            }

            part_category_response = self.api_call('api/v3/lego/part_categories/' + str(id), headers=headers)

            part_category = PartCategory.objects.get_or_create(
                name=part_category_response['name'],
                parent=parent
            )[0]

            self.category_map[id] = part_category

            return part_category

    def import_part(self, part: dict, part_category: PartCategory, set_part: Part):
        print('Processing part ' + str(part['id']))

        # Recupera la categoria generica
        specific_part_category = self.import_category(part['part']['part_cat_id'], part_category)

        part_name = (part['part']['name'][:50] + '..') if len(part['part']['name']) > 50 else part['part']['name']
        part_description = part['part']['name'] if len(part['part']['name']) > 50 else ""
        part_num = part['part']['part_num']

        template_part = Part.objects.get_or_create(
            name=part_name,
            description=part_description,
            IPN=part_num,
            category=specific_part_category,
            is_template=True,
            component = True,
            trackable = True,
            purchaseable = True
        )[0]

        part_name += " " + part['color']['name']
        if part['color']['is_trans']:
            part_name += " Transparent" 

        specific_part = Part.objects.get_or_create(
            name=part_name,
            description=part_description,
            IPN=part_num + "-" + str(part['color']['id']),
            category=specific_part_category,
            variant_of=template_part,
            component = True,
            trackable = True,
            purchaseable = True
        )[0]

        self.import_image(part['part']['part_img_url'], specific_part)

        bom_item = BomItem.objects.get_or_create(
            part=set_part,
            sub_part=specific_part,
            quantity=part['quantity'],
            optional=part['is_spare'],
            consumable=False,
        )[0]

    def import_minifig(self, part: dict, part_category: PartCategory, set_part: Part):
        print('Processing minifig ' + str(part['id']))

        part_name = (part['set_name'][:50] + '..') if len(part['set_name']) > 50 else part['set_name']
        part_description = part['set_name'] if len(part['set_name']) > 50 else ""
        part_num = part['set_num']

        specific_part = Part.objects.get_or_create(
            name=part_name,
            description=part_description,
            IPN=part_num,
            category=part_category,
            component = True,
            trackable = True,
            purchaseable = True
        )[0]

        self.import_image(part['set_img_url'], specific_part)

        bom_item = BomItem.objects.get_or_create(
            part=set_part,
            sub_part=specific_part,
            quantity=part['quantity'],
            optional=False,
            consumable=False,
        )[0]

    def import_parts(self, set_part, part_part_category, url = None):

            headers = {
                "Authorization": "key " + self.get_setting('API_TOKEN')
            }

            if not url:
                num = set_part.IPN
                parts_response = self.api_call('api/v3/lego/sets/' + str(num) + '/parts', headers=headers)

            else:
                parts_response = self.api_call(url, endpoint_is_url=True, headers=headers)

            for part in parts_response['results']:
                self.import_part(part=part, part_category=part_part_category, set_part=set_part)

            if parts_response['next']:
                self.import_parts(set_part, part_part_category, url=parts_response['next'])

    def import_minifigs(self, set_part, minifigs_part_category, url = None):

            headers = {
                "Authorization": "key " + self.get_setting('API_TOKEN')
            }

            if not url:
                num = set_part.IPN
                parts_response = self.api_call('api/v3/lego/sets/' + str(num) + '/minifigs', headers=headers)

            else:
                parts_response = self.api_call(url, endpoint_is_url=True, headers=headers)

            for part in parts_response['results']:
                self.import_minifig(part=part, part_category=minifigs_part_category, set_part=set_part)

            if parts_response['next']:
                self.import_minifigs(set_part, minifigs_part_category, url=parts_response['next'])

    def import_image(self, url:str, part: Part) -> bool:

        if part.image:
            return False

        # URL can be empty (null), for example for stickers parts
        if not url:
            return False

        remote_img = download_image_from_url(url)

        if remote_img and part:
            fmt = remote_img.format or 'PNG'
            buffer = io.BytesIO()
            remote_img.save(buffer, format=fmt)

            # Construct a simplified name for the image
            filename = f'part_{part.pk}_image.{fmt.lower()}'

            part.image.save(filename, ContentFile(buffer.getvalue()))

            return True

        return False

    def import_image_async(self, url, part):
        offload_task(self.import_image, url, part)

    def import_set(self, num: str, category: PartCategory):
        print("Importing LEGO set " + num)

        url ='api/v3/lego/sets/' + str(num)

        headers = {
            "Authorization": "key " + self.get_setting('API_TOKEN')
        }

        set_response = self.api_call(endpoint=url, headers=headers)

        set_part_category = PartCategory.objects.get_or_create(
            name='Sets',
            parent=category
        )[0]

        set_part = Part.objects.get_or_create(
            name=set_response['name'],
            IPN=set_response['set_num'],
            category=set_part_category
        )[0]

        set_part.assembly = True
        set_part.purchaseable = True

        self.import_image(set_response['set_img_url'], set_part)

        set_part.save()

        part_part_category = PartCategory.objects.get_or_create(
            name='Parts',
            parent=category
        )[0]

        self.import_parts(set_part, part_part_category)

        minifigs_part_category = PartCategory.objects.get_or_create(
            name='Minifigs',
            parent=category
        )[0]

        self.import_minifigs(set_part, minifigs_part_category)

    def import_set_async(self, num, category):
        offload_task(self.import_set, num, category)

    @property
    def api_url(self):
        """Base url path."""
        return 'https://rebrickable.com/'

    def perform_action(self, user=None, data=None):
        
        command = data.get('command')

        if command == 'import-set':

            num = data.get('num')
            category = None

            if category_pk := self.get_setting("LEGO_CATEGORY_ID"):
                try:
                    category = PartCategory.objects.get(pk=category_pk)
                except PartCategory.DoesNotExist:
                    category = None

            self.import_set_async(num, category)

        elif command == 'create_part_parameter_templates':
            self.create_part_parameters()

        elif command == 'clear_metadata':
            self.clear_metadata()

        else:
            self.result = {'error'}

    def get_info(self, user, data=None):
        """Sample method."""
        return {'user': user.username, 'hello': 'world'}

    def get_result(self, user=None, data=None):
        """Sample method."""
        return self.result
