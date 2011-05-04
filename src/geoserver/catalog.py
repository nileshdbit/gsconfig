from datetime import datetime, timedelta
import logging
from geoserver.layer import Layer
from geoserver.store import DataStore, CoverageStore, UnsavedDataStore, UnsavedCoverageStore
from geoserver.style import Style
from geoserver.support import prepare_upload_bundle
from geoserver.layergroup import LayerGroup
from geoserver.workspace import Workspace
from os import unlink
import httplib2
from zipfile import is_zipfile
from xml.etree.ElementTree import XML
from urlparse import urlparse
from urllib import urlencode

logger = logging.getLogger("gsconfig.catalog")

class UploadError(Exception):
    pass

class ConflictingDataError(Exception):
    pass

class AmbiguousRequestError(Exception):
    pass

class FailedRequestError(Exception):
    pass

class Catalog(object):
  """
  The GeoServer catalog represents all of the information in the GeoServer
  configuration.  This includes:
  - Stores of geospatial data
  - Resources, or individual coherent datasets within stores
  - Styles for resources
  - Layers, which combine styles with resources to create a visible map layer
  - LayerGroups, which alias one or more layers for convenience
  - Workspaces, which provide logical grouping of Stores
  - Maps, which provide a set of OWS services with a subset of the server's
    Layers
  - Namespaces, which provide unique identifiers for resources
  """

  def __init__(self, url, username="admin", password="geoserver"):
    self.service_url = url
    if self.service_url.endswith("/"):
        self.service_url = self.service_url.strip("/")
    self.http = httplib2.Http()
    self.username = username
    self.password = password
    self.http.add_credentials(self.username, self.password)
    netloc = urlparse(url).netloc
    self.http.authorizations.append(
        httplib2.BasicAuthentication(
            (username, password),
            netloc,
            url,
            {},
            None,
            None,
            self.http
            ))
    self._cache = dict()

  def add(self, object):
    raise NotImplementedError()

  def remove(self, object):
    raise NotImplementedError()

  def delete(self, object, purge=False):
    """
    send a delete request
    XXX [more here]
    """
    url = object.get_url(self.service_url)

    if purge:
        url = url + "?purge=true"

    headers = {
      "Content-type": "application/xml",
      "Accept": "application/xml"
    }
    response = self.http.request(url, "DELETE", headers=headers)
    self._cache.clear()
    return response


  def get_xml(self, url):
    logger.debug("URL: %s", url)
    cached_response = self._cache.get(url)

    def is_valid(cached_response):
        return cached_response is not None and datetime.now() - cached_response[0] < timedelta(seconds=5)

    if is_valid(cached_response):
        return XML(cached_response[1])
    else:
        response, content = self.http.request(url)
        if response.status == 200:
            self._cache[url] = (datetime.now(), content)
            return XML(content)
        else:
            raise FailedRequestError("Tried to make a GET request to %s but got a %d status code: \n%s" % (url, response.status, content))

  def save(self, object):
    """
    saves an object to the REST service

    gets the object's REST location and the XML from the object,
    then POSTS the request.
    """
    url = object.get_url(self.service_url)
    message = object.serialize()
    headers = {
      "Content-type": "application/xml",
      "Accept": "application/xml"
    }
    response = self.http.request(url, object.save_method, message, headers)
    self._cache.clear()
    return response

  def get_store(self, name, workspace=None):
      #stores = [s for s in self.get_stores(workspace) if s.name == name]
      if workspace is None:
          store = None
          for ws in self.get_workspaces():
              found = None
              try:
                  found = self.get_store(name, ws)
              except:
                  # don't expect every workspace to contain the named store
                  pass
              if found:
                  if store:
                      raise AmbiguousRequestError("Multiple stores found named: " + name)
                  else:
                      store = found

          if not store:
              raise FailedRequestError("No store found named: " + name)
          return store
      else: # workspace is not None
          logger.debug("datastore url is [%s]", workspace.datastore_url )
          ds_list = self.get_xml(workspace.datastore_url)
          cs_list = self.get_xml(workspace.coveragestore_url)
          datastores = [n for n in ds_list.findall("dataStore") if n.find("name").text == name]
          coveragestores = [n for n in cs_list.findall("coverageStore") if n.find("name").text == name]
          ds_len, cs_len = len(datastores), len(coveragestores)

          if ds_len == 1 and cs_len == 0:
              return DataStore(self, datastores[0])
          elif ds_len == 0 and cs_len == 1:
              return CoverageStore(self, coveragestores[0])
          elif ds_len == 0 and cs_len == 0:
              raise FailedRequestError("No store found in " + str(workspace) + " named: " + name)
          else:
              raise AmbiguousRequestError(str(workspace) + " and name: " + name + " do not uniquely identify a layer")

  def get_stores(self, workspace=None):
      if workspace is not None:
          ds_list = self.get_xml(workspace.datastore_url)
          cs_list = self.get_xml(workspace.coveragestore_url)
          datastores = [DataStore(self, n) for n in ds_list.findall("dataStore")]
          coveragestores = [CoverageStore(self, n) for n in cs_list.findall("coverageStore")]
          return datastores + coveragestores
      else:
          stores = []
          for ws in self.get_workspaces():
              a = self.get_stores(ws)
              stores.extend(a)
          return stores

  def create_datastore(self, name, workspace = None):
      if workspace is None:
          workspace = self.get_default_workspace()
      return UnsavedDataStore(self, name, workspace)

  def create_coveragestore2(self, name, workspace = None):
      """
      Hm we already named the method that creates a coverage *resource*
      create_coveragestore... time for an API break?
      """
      if workspace is None:
          workspace = self.get_default_workspace()
      return UnsavedCoverageStore(self, name, workspace)

  def add_data_to_store(self, store, name, data, overwrite = False, charset = None):
      if isinstance(data, dict):
          bundle = prepare_upload_bundle(name, data)
      else:
          bundle = data

      params = dict()
      if overwrite:
          params["overwrite"] = True
      if charset is not None:
          params["charset"] = charset

      if len(params):
          params = "?" + urlencode(params)
      else:
          params = ""

      message = open(bundle)
      headers = { 'Content-Type': 'application/zip', 'Accept': 'application/xml' }
      url = "%s/workspaces/%s/datastores/%s/file.shp%s" % (
              self.service_url, store.workspace.name, store.name, params)

      try:
          headers, response = self.http.request(url, "PUT", message, headers)
          self._cache.clear()
          if headers.status != 201:
              raise UploadError(response)
      finally:
          unlink(bundle)

  def create_pg_feature(self, storeXML, name, data, workspace=None, overwrite=False, charset=None):

    if not overwrite:
        try:
            layer = self.get_layer(name)
            if layer:
                msg = "There is already a layer named " + name
                if workspace:
                    msg += " in " + str(workspace)
                raise ConflictingDataError(msg)
        except FailedRequestError, e:
            # we don't really expect that every layer name will be taken
            pass

    if workspace is None:
      workspace = self.get_default_workspace()

    message = storeXML

    #Create new PostGIS datastore (only if this is a new layer)
    if not overwrite:
        store_url = "%s/workspaces/%s/datastores" % (self.service_url, workspace.name)
        headers = {
        "Content-type": "text/xml",
        "Accept": "application/xml"
        }
        try:
            logger.debug("Attempt GS PostGIS store creation")
            headers, response = self.http.request(store_url, "POST", message, headers)
            self._cache.clear()
            if headers.status != 201:
                logger.error('Store creation failed: %s', headers.status)
                raise UploadError(response)
        except Exception, ex:
            logger.error('Store creation failed: %s', str(ex))
            raise UploadError(ex)

    #Create/update layer
    if charset:
        ds_url = "%s/workspaces/%s/datastores/%s/file.shp?charset=%s" % (self.service_url, workspace.name, name, charset)
    else:
        ds_url = "%s/workspaces/%s/datastores/%s/file.shp" % (self.service_url, workspace.name, name)

    if overwrite:
        ds_url = ds_url + ("&" if charset else "?") + "update=overwrite"


    headers = {
      "Content-type": "application/zip",
      "Accept": "application/xml"
    }

    logger.debug("Upload GS URL is [%s]", ds_url)
    if  isinstance(data,dict):
        logger.debug('Data is NOT a zipfile')
        zip = prepare_upload_bundle(name, data)
    else:
        logger.debug('Data is a zipfile')
        zip = data
    message = open(zip)
    try:
      logger.debug("Attempt GS import")
      headers, response = self.http.request(ds_url, "PUT", message, headers)
      self._cache.clear()
      if headers.status != 201:
          raise UploadError(response)
    finally:
      if zip:
        unlink(zip)

  def create_featurestore(self, name, data, workspace=None, overwrite=False, charset=None):
    if not overwrite:
        try:
            store = self.get_store(name, workspace)
            msg = "There is already a store named " + name
            if workspace:
                msg += " in " + str(workspace)
            raise ConflictingDataError(msg)
        except FailedRequestError, e:
            # we don't really expect that every layer name will be taken
            pass

    if workspace is None:
      workspace = self.get_default_workspace()
    if charset:
        ds_url = "%s/workspaces/%s/datastores/%s/file.shp?charset=%s" % (self.service_url, workspace.name, name, charset)
    else:
        ds_url = "%s/workspaces/%s/datastores/%s/file.shp" % (self.service_url, workspace.name, name)

    # PUT /workspaces/<ws>/datastores/<ds>/file.shp
    headers = {
      "Content-type": "application/zip",
      "Accept": "application/xml"
    }
    if  isinstance(data,dict):
        logger.debug('Data is NOT a zipfile')
        zip = prepare_upload_bundle(name, data)
    else:
        logger.debug('Data is a zipfile')
        zip = data
    message = open(zip)
    try:
      headers, response = self.http.request(ds_url, "PUT", message, headers)
      self._cache.clear()
      if headers.status != 201:
          raise UploadError(response)
    finally:
      unlink(zip)

  def create_coveragestore(self, name, data, workspace=None, overwrite=False):
    if not overwrite:
        try:
            store = self.get_store(name, workspace)
            msg = "There is already a store named " + name
            if workspace:
                msg += " in " + str(workspace)
            raise ConflictingDataError(msg)
        except FailedRequestError, e:
            # we don't really expect that every layer name will be taken
            pass

    if workspace is None:
      workspace = self.get_default_workspace()
    headers = {
      "Content-type": "image/tiff",
      "Accept": "application/xml"
    }

    zip = None
    ext = "geotiff"

    if isinstance(data, dict):
      zip = prepare_upload_bundle(name, data)
      message = open(zip)
      if "tfw" in data:
        headers['Content-type'] = 'application/zip'
        ext = "worldimage"
    elif isinstance(data, basestring):
      message = open(data)
    else:
      message = data

    cs_url = "%s/workspaces/%s/coveragestores/%s/file.%s" % (self.service_url, workspace.name, name, ext)
    try:
      headers, response = self.http.request(cs_url, "PUT", message, headers)
      self._cache.clear()
      if headers.status != 201:
          raise UploadError(response)
    finally:
      if zip is not None:
        unlink(zip)

  def get_resource(self, name, store=None, workspace=None):
    if store is not None:
      candidates = filter(lambda x: x.name == name, self.get_resources(store))
      if len(candidates) == 0:
        return None
      elif len(candidates) > 1:
        raise AmbiguousRequestError
      else:
        return candidates[0]

    if workspace is not None:
      for store in self.get_stores(workspace):
        resource = self.get_resource(name, store)
        if resource is not None:
          return resource
      return None

    for ws in self.get_workspaces():
      resource = self.get_resource(name, workspace=ws)
      if resource is not None:
        return resource
    return None

  def get_resources(self, store=None, workspace=None, namespace=None):
    if store is not None:
      return store.get_resources()
    if workspace is not None:
      resources = []
      for store in self.get_stores(workspace):
          resources.extend(self.get_resources(store))
      return resources
    resources = []
    for ws in self.get_workspaces():
      resources.extend(self.get_resources(workspace=ws))
    return resources

  def get_layer(self, name=None):
    description = self.get_xml("%s/layers.xml" % self.service_url)
    layers = [l for l in description.findall("layer") if l.find("name").text == name]
    if len(layers) == 0:
      return None
    elif len(layers) > 1:
      raise AmbiguousRequestError("%s does not uniquely identify a layer" % name)
    else:
      return Layer(self, layers[0])

  def get_layers(self, resource=None, style=None):
    description = self.get_xml("%s/layers.xml" % self.service_url)
    lyrs = [Layer(self, l) for l in description.findall("layer")]
    if resource is not None:
      lyrs = [l for l in lyrs if l.resource.href == resource.href]
    # TODO: Filter by style
    return lyrs

  def get_maps(self):
    raise NotImplementedError()

  def get_map(self, id=None, name=None):
    raise NotImplementedError()

  def get_layergroup(self, id=None, name=None):
    group = self.get_xml("%s/layergroups/%s.xml" % (self.service_url, name))
    return LayerGroup(self, group.find("name").text)

  def get_layergroups(self):
    groups = self.get_xml("%s/layergroups.xml" % self.service_url)
    return [LayerGroup(self,group) for group in groups.findall("layerGroup")]

  def get_style(self, name):
    description = self.get_xml("%s/styles.xml" % self.service_url)
    candidates = [l for l in description.findall("style") if l.find("name").text == name]
    if len(candidates) == 0:
      return None
    elif len(candidates) > 1:
      raise AmbiguousRequestError()
    else:
      return Style(self, candidates[0])

  def get_styles(self):
    description = self.get_xml("%s/styles.xml" % self.service_url)
    return [Style(self,s) for s in description.findall("style")]

  def create_style(self, name, data, overwrite = False):
    if overwrite == False and self.get_style(name) is not None:
      raise ConflictingDataError("There is already a style named %s" % name)

    headers = {
      "Content-type": "application/vnd.ogc.sld+xml",
      "Accept": "application/xml"
    }

    if overwrite:
      style_url = "%s/styles/%s.sld" % (self.service_url, name)
      headers, response = self.http.request(style_url, "PUT", data, headers)
    else:
      style_url = "%s/styles?name=%s" % (self.service_url, name)
      headers, response = self.http.request(style_url, "POST", data, headers)

    self._cache.clear()
    if headers.status < 200 or headers.status > 299: raise UploadError(response)

  def get_namespace(self, id=None, prefix=None, uri=None):
    raise NotImplementedError()

  def get_default_namespace(self):
    raise NotImplementedError()

  def set_default_namespace(self):
    raise NotImplementedError()

  def create_workspace(self, name, uri):
    xml = ("<namespace>"
          "<prefix>{name}</prefix>"
          "<uri>{uri}</uri>"
          "</namespace>").format(name=name, uri=uri)
    headers = { "Content-Type": "application/xml" }
    workspace_url = self.service_url + "/namespaces/"

    headers, response = self.http.request(workspace_url, "POST", xml, headers)
    assert 200 <= headers.status < 300, "Tried to create workspace but got " + str(headers.status) + ": " + response
    return self.get_workspace(name)

  def get_workspaces(self):
    description = self.get_xml("%s/workspaces.xml" % self.service_url)
    return [Workspace(self, node) for node in description.findall("workspace")]

  def get_workspace(self, name):
    candidates = filter(lambda x: x.name == name, self.get_workspaces())
    if len(candidates) == 0:
      return None
    elif len(candidates) > 1:
      raise AmbiguousRequestError()
    else:
      return candidates[0]

  def reassign_workspace(self, store, workspace):
    def _create_forcing_workspace(self, store, workspace):
      pass
    def _dupe_resource(res, newstore): pass
    newstore = _create_forcing_workspace(self, store, workspace)
    for res in cat.get_resources(store):
      dupe_resource(res, newstore)
    self.delete(store, purge=True)

  def get_default_workspace(self):
      return Workspace(self, XML("""
          <workspace>
              <atom:link xmlns:atom="%s" href="%s/workspaces/default.xml"></atom:link>
          </workspace>
      """ % ("http://www.w3.org/2005/Atom", self.service_url)
      ))

  def set_default_workspace(self):
    raise NotImplementedError()
