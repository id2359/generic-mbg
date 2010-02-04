# MayaVI's mlab documentation is at
# http://code.enthought.com/projects/mayavi/docs/development/html/mayavi/mlab.html#simple-scripting-with-mlab

from enthought.mayavi import mlab
import tables as tb
import numpy as np

# The 'colormap' trait of a VolumeFactory instance must be 'Accent' or 'Blues' or 'BrBG' or
# 'BuGn' or 'BuPu' or 'Dark2' or 'GnBu' or 'Greens' or 'Greys' or 'OrRd' or 'Oranges' or
# 'PRGn' or 'Paired' or 'Pastel1' or 'Pastel2' or 'PiYG' or 'PuBu' or 'PuBuGn' or 'PuOr' or
# 'PuRd' or 'Purples' or 'RdBu' or 'RdGy' or 'RdPu' or 'RdYlBu' or 'RdYlGn' or 'Reds' or
# 'Set1' or 'Set2' or 'Set3' or 'Spectral' or 'YlGn' or 'YlGnBu' or 'YlOrBr' or 'YlOrRd' or
# 'autumn' or 'black-white' or 'blue-red' or 'bone' or 'cool' or 'copper' or 'file' or
# 'flag' or 'gist_earth' or 'gist_gray' or 'gist_heat' or 'gist_ncar' or 'gist_rainbow' or
# 'gist_stern' or 'gist_yarg' or 'gray' or 'hot' or 'hsv' or 'jet' or 'pink' or 'prism' or
# 'spring' or 'summer' or 'winter'


# Opens the HDF5 file containing the scalar density field
d = tb.openFile('3dmap-data.hdf5')

def add_map(colormap='Accent', opacity=1):
    "Adds the land/water mask to the 3d mask as a basemap."
    # The maximum x and y values are equal to the number of rows and columns,
    # respectively, in the mask raster. This is done to make the map line up with
    # the scalar density field.
    mlab.imshow(d.root.unmasked[:].astype('float'),
                extent=[0,d.root.unmasked.shape[0],0,d.root.unmasked.shape[1],0,1], 
                colormap='Accent',
                opacity=opacity)

def add_cutplane(colormap='hot', plane_orientation='x_axes', vmin=0, vmax=1):
    """
    Adds a cutplane, with given orientation, to the map.
    
    This is the most complete summary of the per-pixel posterior predictive
    distribution generated by any of the products of generic-mbg.
    """
    mlab.pipeline.image_plane_widget(mlab.pipeline.scalar_field(d.root.density_field[:]),
                                plane_orientation=plane_orientation,
                                colormap=colormap,
                                vmin=vmin,
                                vmax=vmax)
                                
def add_cloud(colormap='hot',vmin=0,vmax=.01):
    "Visualizes the density as a cloud. This feature of mayaVI seems a little buggy."
    mlab.pipeline.volume(mlab.pipeline.scalar_field(d.root.density_field[:]*.999+.001), 
                            colormap=colormap,
                            vmin=vmin, 
                            vmax=vmax)

def add_mean(color=(.9,0,.3), opacity=.3):
    "Adds the mean to the map as a 3d surface."
    mlab.surf(d.root.mean[:],
                # The maximum x and y values are equal to the number of rows and columns,
                # respectively, in the mask raster. The maximum z value is equal to the 
                # number of bins in the density field. This is done to make the surface line 
                # up with the scalar density field.                
                extent=[0,d.root.unmasked.shape[0],0,d.root.unmasked.shape[1],0,d.root.density_field.shape[-1]*d.root.mean[:].max()], 
                color=color,
                # colormap='Accent',
                opacity=opacity)



def add_quantiles(quantiles=[.25, .5, .75], colors=None, opacities = None):
    """
    Adds the given quantiles to the map as 3d surfaces. 
    
    Colors and opacities can be set individually.
    """
    # Default color and opacity values.
    if colors is None:
        colors = [(.9,0,.3)]*len(quantiles)
    if opacities is None:
        opacities = [.3]*len(quantiles)
    
    # Get the empirical CDF of each pixel.
    cu_data = np.cumsum(d.root.density_field[:], axis=-1)
    cu_data /= cu_data.max()
    
    for quantile,color,opacity in zip(quantiles,colors,opacities):
        # Compute the actual quantile surface.
        # FIXME: Use histogram_finalize for speed
        s = np.zeros(d.root.unmasked.shape)
        for i in xrange(cu_data.shape[0]):
            for j in xrange(cu_data.shape[1]):
                for k in xrange(cu_data.shape[2]):
                    if cu_data[i,j,k]>quantile:
                        s[i,j] = k
                        break
        # Normalize
        s /= cu_data.shape[2]
        mlab.surf(s,
                # The maximum x and y values are equal to the number of rows and columns,
                # respectively, in the mask raster. The maximum z value is equal to the 
                # number of bins in the density field. This is done to make the surface line 
                # up with the scalar density field.                            
                extent=[0,d.root.unmasked.shape[0],0,d.root.unmasked.shape[1],0,d.root.density_field.shape[-1]*s.max()], 
                color=color,
                # colormap='Accent',
                opacity=opacity)

def get_datapoints(clip=True):
    "Returns lon, lat, z of the datapoints, scaled so that mlab.points3d(lon,lat,z) looks good."
    
    lon = d.root.data[:,0]
    lat = d.root.data[:,1]
    
    # If the clip argument is set, only keep the data locations that lie within the 'unmasked' raster.
    if clip:
        where_in = np.where((lon>=d.root.bbox[0])*(lon<=d.root.bbox[2])*(lat>=d.root.bbox[1])*(lat<=d.root.bbox[3]))
    else:
        where_in = None

    # Scale the longitude and latitude values so that the datapoints line up with the
    # density field, base map, mean & quantile surfaces etc.
    lon = (lon-d.root.bbox[0])*d.root.unmasked.shape[0]/(d.root.bbox[2]-d.root.bbox[0])
    lat = (lat-d.root.bbox[1])*d.root.unmasked.shape[1]/(d.root.bbox[3]-d.root.bbox[1])
    
    z = (d.root.data[:,2])*d.root.density_field.shape[-1]
    return lon[where_in],lat[where_in],z[where_in]

def add_datapoints(color=(0,.9,.3), mode='sphere', scale_factor=1, clip=True):
    "Adds the datapoints to the 3d map."
    lon,lat,z = get_datapoints(clip)
    mlab.points3d(lon,lat,z, color=color, mode=mode, scale_factor=scale_factor)


if __name__ == '__main__':
    # Examples.
    mlab.figure(bgcolor=(1,1,1))
    add_cloud(vmax=.1)
    add_mean(color=(.3,0,.9), opacity=.3)
    add_quantiles([.5])
    add_cutplane(vmax=.1,plane_orientation='x_axes')
    add_cutplane(vmax=.1,plane_orientation='y_axes')
    add_map(opacity=.3)
    add_datapoints(scale_factor=1)
    lon,lat,z = get_datapoints()

